# Adapted from
# https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/hunyuandit/pipeline_hunyuandit.py#L141

import torch
from diffusers import HunyuanDiTPipeline
from diffusers.models.transformers.hunyuan_transformer_2d import HunyuanDiT2DModel

from pipefuser.pipelines.pip.distri_hunyuandit import DistriHunyuanDiTPiP
from pipefuser.schedulers.pip.dpmsolver_multistep import DPMSolverMultistepSchedulerPiP
from pipefuser.schedulers.pip.ddim import DDIMSchedulerPiP
from pipefuser.schedulers.pip.ddpm import DDPMSchedulerPiP
from pipefuser.models import (
    NaivePatchDiT,
    DistriDiTPP,
    DistriDiTHunyuanPipeFusion,
    DistriDiTTP,
)
from pipefuser.utils import (
    DistriConfig,
    PatchParallelismCommManager,
    PipelineParallelismCommManager,
)
from pipefuser.logger import init_logger

from distvae.modules.adapters.vae.decoder_adapters import DecoderAdapter

logger = init_logger(__name__)


class DistriHunyuanDiTPipeline:
    def __init__(
        self,
        pipeline: HunyuanDiTPipeline,
        module_config: DistriConfig,
        enable_parallel_vae: bool = False,
        use_profiler: bool = False,
    ):
        self.pipeline = pipeline
        if enable_parallel_vae and self.pipeline.vae is not None:
            self.pipeline.vae.decoder = DecoderAdapter(
                self.pipeline.vae.decoder, use_profiler=use_profiler
            )

        # assert module_config.do_classifier_free_guidance == False
        # assert module_config.split_batch == False

        self.distri_config = module_config

        self.static_inputs = None

        self.prepare()

    @staticmethod
    def from_pretrained(distri_config: DistriConfig, **kwargs):
        device = distri_config.device
        pretrained_model_name_or_path = kwargs.pop(
            "pretrained_model_name_or_path", "Tencent-Hunyuan/HunyuanDiT-v1.2-Diffusers"
        )
        torch_dtype = kwargs.pop("torch_dtype", torch.float16)
        enable_parallel_vae = kwargs.pop("enable_parallel_vae", False)
        use_profiler = kwargs.pop("use_profiler", False)
        transformer = HunyuanDiT2DModel.from_pretrained(
            pretrained_model_name_or_path,
            torch_dtype=torch_dtype,
            subfolder="transformer",
        )

        if distri_config.parallelism == "origin":
            pass
        elif (
            distri_config.parallelism == "patch"
            or distri_config.parallelism == "sequence"
        ):
            raise ValueError("Patch parallelism is not supported for HunyuanDiT")
            # transformer = DistriDiTPP(transformer, distri_config)
        elif distri_config.parallelism == "naive_patch":
            raise ValueError("Naive patch parallelism is not supported for HunyuanDiT")
            # transformer = NaivePatchDiT(transformer, distri_config)
        elif distri_config.parallelism == "pipefusion":
            transformer = DistriDiTHunyuanPipeFusion(transformer, distri_config)
        elif distri_config.parallelism == "tensor":
            raise ValueError("Tensor parallelism is not supported for HunyuanDiT")
            # transformer = DistriDiTTP(transformer, distri_config)
        else:
            raise ValueError(f"Unknown parallelism: {distri_config.parallelism}")

        peak_memory = torch.cuda.max_memory_allocated(device="cuda")
        print(f"DistriHunyuanDiTPipeline from pretrain stage 1 {peak_memory/1e9} GB")

        if distri_config.parallelism == "pipefusion":
            if distri_config.scheduler == "dpm-solver":
                scheduler = DPMSolverMultistepSchedulerPiP.from_pretrained(
                    pretrained_model_name_or_path, subfolder="scheduler"
                )
            elif distri_config.scheduler == "ddim":
                scheduler = DDIMSchedulerPiP.from_pretrained(
                    pretrained_model_name_or_path, subfolder="scheduler"
                )
            elif distri_config.scheduler == "ddpm":
                scheduler = DDPMSchedulerPiP.from_pretrained(
                    pretrained_model_name_or_path, subfolder="scheduler"
                )
            else:
                raise ValueError(
                    f"scheduler do not support in pipefusion paralleliem: {distri_config.scheduler}"
                )
            scheduler.init(distri_config)

        if distri_config.parallelism == "pipefusion":
            pipeline = DistriHunyuanDiTPiP.from_pretrained(
                pretrained_model_name_or_path,
                torch_dtype=torch_dtype,
                transformer=transformer,
                scheduler=scheduler,
                **kwargs,
            ).to(device)
            pipeline.init(distri_config)
        else:
            pipeline = HunyuanDiTPipeline.from_pretrained(
                pretrained_model_name_or_path,
                torch_dtype=torch_dtype,
                transformer=transformer,
                **kwargs,
            ).to(device)

        peak_memory = torch.cuda.max_memory_allocated(device="cuda")
        print(f"DistriHunyuanDiTPipeline from pretrain stage 2 {peak_memory/1e9} GB")
        
        if distri_config.parallelism == "origin":
            return pipeline

        ret = DistriHunyuanDiTPipeline(
            pipeline,
            distri_config,
            enable_parallel_vae=enable_parallel_vae,
            use_profiler=use_profiler,
        )

        peak_memory = torch.cuda.max_memory_allocated(device="cuda")
        print(f"DistriHunyuanDiTPipeline from pretrain stage 3 {peak_memory/1e9} GB")
        return ret

    def set_progress_bar_config(self, **kwargs):
        self.pipeline.set_progress_bar_config(**kwargs)

    @torch.no_grad()
    def __call__(self, prompt, num_inference_steps=20, *args, **kwargs):
        assert "height" not in kwargs, "height should not be in kwargs"
        assert "width" not in kwargs, "width should not be in kwargs"
        self.distri_config.num_inference_steps = num_inference_steps
        self.pipeline.transformer.set_counter(0)
        config = self.distri_config
        return self.pipeline(
            height=config.height,
            width=config.width,
            prompt=prompt,
            use_resolution_binning=config.use_resolution_binning,
            num_inference_steps=num_inference_steps,
            *args,
            **kwargs,
        )

    @torch.no_grad()
    def prepare(self, **kwargs):
        distri_config = self.distri_config

        static_inputs = {}
        static_outputs = []
        cuda_graphs = []
        pipeline = self.pipeline

        height = distri_config.height
        width = distri_config.width
        assert height % 8 == 0 and width % 8 == 0

        device = distri_config.device

        batch_size = distri_config.batch_size or 1
        prompt = [""] * batch_size if batch_size > 1 else ""
        num_images_per_prompt = 1

        if distri_config.parallelism == "pipefusion":
            comm_manager = PipelineParallelismCommManager(distri_config)
            self.pipeline.set_comm_manager(comm_manager)
            self.pipeline(
                height=distri_config.height,
                width=distri_config.width,
                prompt=prompt,
                use_resolution_binning=distri_config.use_resolution_binning,
                num_inference_steps=distri_config.warmup_steps + 2,
                output_type="latent",
                generator=torch.Generator(device="cuda").manual_seed(42),
            )
        
        elif distri_config.parallelism == "origin":
            self.pipeline(
                height=distri_config.height,
                width=distri_config.width,
                prompt=prompt,
                use_resolution_binning=distri_config.use_resolution_binning,
                num_inference_steps=distri_config.warmup_steps + 2,
                output_type="latent",
                generator=torch.Generator(device="cuda").manual_seed(42),
            )

        else:
            raise NotImplementedError(
                "HunyuanDiT doesn't support other parallelism methods now"
            )
            # Resolution binning
            if distri_config.use_resolution_binning:
                if pipeline.transformer.config.sample_size == 128:
                    aspect_ratio_bin = ASPECT_RATIO_1024_BIN
                elif pipeline.transformer.config.sample_size == 64:
                    aspect_ratio_bin = ASPECT_RATIO_512_BIN
                elif pipeline.transformer.config.sample_size == 32:
                    aspect_ratio_bin = ASPECT_RATIO_256_BIN
                else:
                    raise ValueError("Invalid sample size")
                orig_height, orig_width = height, width
                height, width = pipeline.height, width = (
                    pipeline.image_processor.classify_height_width_bin(
                        height, width, ratios=aspect_ratio_bin
                    )
                )

            # Encode input prompt
            (
                prompt_embeds,
                prompt_attention_mask,
                negative_prompt_embeds,
                negative_prompt_attention_mask,
            ) = self.pipeline.encode_prompt(
                prompt=prompt,
                do_classifier_free_guidance=distri_config.do_classifier_free_guidance,
                device=device,
            )

            if distri_config.do_classifier_free_guidance:
                if not distri_config.split_batch:
                    prompt_embeds = torch.cat(
                        [negative_prompt_embeds, prompt_embeds], dim=0
                    )
                    prompt_attention_mask = torch.cat(
                        [negative_prompt_attention_mask, prompt_attention_mask], dim=0
                    )
                else:
                    if distri_config.batch_idx() == 0:
                        prompt_embeds = negative_prompt_embeds
                        prompt_attention_mask = negative_prompt_attention_mask
                    elif distri_config.batch_idx() == 1:
                        prompt_embeds = prompt_embeds
                        prompt_attention_mask = prompt_attention_mask
                    else:
                        raise ValueError("Invalid batch_idx")
                        

            # Prepare added time ids & embeddings

            t = torch.zeros([2], device=device, dtype=torch.long)

            guidance_scale = 4.0

            latent_channels = pipeline.transformer.config.in_channels
            latents = pipeline.prepare_latents(
                batch_size,
                latent_channels,
                height,
                width,
                prompt_embeds.dtype,
                device,
                None,
            )
            latent_model_input = (
                torch.cat([latents, latents], 0) 
                if distri_config.do_classifier_free_guidance and guidance_scale > 1 and not distri_config.split_batch
                else latents
            )

            # encoder_hidden_states.shape torch.Size([2, 120, 4096])
            # encoder_attention_mask.shape torch.Size([2, 120])
            # resolution.shape torch.Size([2, 2])
            # aspect_ratio.shape torch.Size([2, 1])
            static_inputs["hidden_states"] = latent_model_input
            static_inputs["timestep"] = t
            static_inputs["encoder_hidden_states"] = prompt_embeds
            static_inputs["encoder_attention_mask"] = prompt_attention_mask
            added_cond_kwargs = {"resolution": None, "aspect_ratio": None}
            if pipeline.transformer.config.sample_size == 128:
                resolution = torch.tensor([0, 0]).repeat(
                    batch_size * num_images_per_prompt, 1
                )
                aspect_ratio = torch.tensor([0.0]).repeat(
                    batch_size * num_images_per_prompt, 1
                )
                resolution = resolution.to(dtype=prompt_embeds.dtype, device=device)
                aspect_ratio = aspect_ratio.to(dtype=prompt_embeds.dtype, device=device)

                if distri_config.do_classifier_free_guidance and not distri_config.split_batch:
                    resolution = torch.cat([resolution, resolution], dim=0)
                    aspect_ratio = torch.cat([aspect_ratio, aspect_ratio], dim=0)

                added_cond_kwargs = {
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                }
            static_inputs["added_cond_kwargs"] = added_cond_kwargs

            # Used to create communication buffer
            comm_manager = None
            if distri_config.n_device_per_batch > 1:
                comm_manager = PatchParallelismCommManager(distri_config)
                pipeline.transformer.set_comm_manager(comm_manager)

                # Only used for creating the communication buffer
                pipeline.transformer.set_counter(0)
                pipeline.transformer(**static_inputs, return_dict=False, record=True)
                if comm_manager.numel > 0:
                    comm_manager.create_buffer()

            # Pre-run
            pipeline.transformer.set_counter(0)
            pipeline.transformer(**static_inputs, return_dict=False, record=True)

            if distri_config.use_cuda_graph:
                if comm_manager is not None:
                    comm_manager.clear()
                if distri_config.parallelism == "naive_patch":
                    counters = [0, 1]
                elif (
                    distri_config.parallelism == "patch"
                    or distri_config.parallelism == "sequence"
                ):
                    counters = [
                        0,
                        distri_config.warmup_steps + 1,
                        distri_config.warmup_steps + 2,
                    ]
                else:
                    raise ValueError(
                        f"Unknown parallelism: {distri_config.parallelism}"
                    )
                for counter in counters:
                    graph = torch.cuda.CUDAGraph()
                    with torch.cuda.graph(graph):
                        pipeline.transformer.set_counter(counter)
                        output = pipeline.transformer(
                            **static_inputs, return_dict=False, record=True
                        )[0]
                        static_outputs.append(output)
                    cuda_graphs.append(graph)
                pipeline.transformer.setup_cuda_graph(static_outputs, cuda_graphs)

            self.static_inputs = static_inputs

        peak_memory = torch.cuda.max_memory_allocated(device="cuda")
        print(f"memory before prepare {peak_memory/1e9} GB")
