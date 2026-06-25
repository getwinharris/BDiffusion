# BDiffusion

Status: renamed/current ByteOmniDiffus research repo.

This repository currently contains the imported L2D codebase from the initial commit. That code is reference material for diffusion-style adaptation, not the final product identity.

## Active direction

BDiffusion is being rebuilt around combined remote model-weight records treated as dense diffusion-style model objects.

A model record groups related artifacts together:

- GGUF / ONNX / safetensors weights
- Q8 quantization metadata
- config files
- tokenizer files
- model cards
- dataset links
- source URLs
- capability tags

The target system is not a next-token prediction pipeline. It is a diffusion-style routing and refinement system over combined model-state records.

## What to keep from L2D

Keep the useful mechanism idea:

- a base model and tokenizer are linked together
- a diffusion or flow layer can adapt/refine over the base model
- configs can describe model identity, flow representation, timestep behavior, LoRA/adaptation, and evaluation

Do not treat the imported L2D README, branding, training commands, or dependency stack as BDiffusion product identity.

## First rebuild pieces

- combined model record schema
- remote artifact indexer
- SearXNG search integration
- direct URL and Hugging Face artifact discovery
- Q8 metadata scanner
- capability router
- generated project map

## Repository rule

Keep this repo small and understandable. Remove or quarantine anything that does not support the new BDiffusion direction.
