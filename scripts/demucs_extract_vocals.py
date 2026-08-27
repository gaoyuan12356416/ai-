#!/usr/bin/env python3
"""Demucs WAV adapter preserving the legacy GPU worker's command protocol.

Adapted from the read-only 43.166.178.132 script, SHA-256
604b0f3dd9db93024c17f2cdb9974a6ac5021cf71cb0a726ffbe1bb2c96d0e68.
The optional local repository is the only model-resolution change. Imports are
lazy so --help and argument-contract tests need no GPU or third-party packages.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path


def load_wav(path: Path, channels: int, samplerate: int):
    import soundfile as sf
    import torch as th

    audio, sr = sf.read(str(path), always_2d=True, dtype="float32")
    if sr != samplerate:
        raise ValueError(f"Expected {samplerate} Hz WAV, got {sr} Hz: {path}")
    wav = th.from_numpy(audio).t()
    if wav.shape[0] == channels:
        return wav
    if wav.shape[0] == 1 and channels == 2:
        return wav.repeat(2, 1)
    if wav.shape[0] > channels:
        return wav[:channels]
    pad = wav[-1:, :].repeat(channels - wav.shape[0], 1)
    return th.cat([wav, pad], dim=0)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract vocals from an audio file with Demucs and save as WAV."
    )
    parser.add_argument("input", type=Path, help="Input audio path")
    parser.add_argument("output", type=Path, help="Output vocals WAV path")
    parser.add_argument("-n", "--name", default="htdemucs", help="Demucs model name. Default: htdemucs")
    parser.add_argument("-d", "--device", default=None, help="Inference device. Default: cuda if available, else cpu")
    parser.add_argument("--shifts", type=int, default=1, help="Shift augmentation count")
    parser.add_argument("--overlap", type=float, default=0.25, help="Chunk overlap ratio")
    parser.add_argument("--segment", type=int, default=None, help="Chunk length in seconds for transformer models")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="Parallel worker count on CPU")
    parser.add_argument("--repo", type=Path, default=None, help="Offline model directory; defaults to DEMUCS_MODEL_REPO")
    return parser.parse_args(argv)


def resolve_model_repo(repo=None, *, environ=None):
    env = os.environ if environ is None else environ
    raw = str(repo if repo is not None else env.get("DEMUCS_MODEL_REPO", "")).strip()
    required = str(env.get("DEMUCS_REQUIRE_LOCAL_MODELS", "0")).strip() == "1"
    if not raw:
        if required:
            raise ValueError("DEMUCS_MODEL_REPO is required for offline inference")
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("DEMUCS_MODEL_REPO must be an existing absolute directory")
    return path.resolve(strict=True)


def normalization_parameters(reference_mean, reference_std, signal_std):
    """Keep silence and anti-phase stereo finite without discarding channels."""
    mean, reference_scale, signal_scale = map(
        float, (reference_mean, reference_std, signal_std)
    )
    if not all(math.isfinite(value) for value in (mean, reference_scale, signal_scale)):
        raise ValueError("Input audio contains non-finite samples")
    if reference_scale < 0 or signal_scale < 0:
        raise ValueError("Input audio has an invalid normalization scale")
    scale = reference_scale if reference_scale > 1e-8 else signal_scale
    # L=-R has a silent mono reference, but a nonzero all-channel scale.
    # Only constant/near-silent input reaches the unit-scale fallback.
    return mean, scale if scale > 1e-8 else 1.0


def main(argv=None) -> None:
    args = parse_args(argv)
    repo = resolve_model_repo(args.repo)

    import soundfile as sf
    import torch as th
    from demucs.apply import BagOfModels, apply_model
    from demucs.pretrained import get_model

    device = args.device or ("cuda" if th.cuda.is_available() else "cpu")
    model = get_model(args.name, repo=repo) if repo is not None else get_model(args.name)
    if isinstance(model, BagOfModels):
        print(f"Selected bag with {len(model.models)} model(s).")
    model.cpu()
    model.eval()

    wav = load_wav(args.input, model.audio_channels, model.samplerate)
    if not wav.numel():
        raise ValueError("Input audio is empty")
    ref = wav.mean(0)
    offset, scale = normalization_parameters(
        ref.mean(), ref.std(unbiased=False), wav.std(unbiased=False)
    )
    wav = (wav - offset) / scale
    sources = apply_model(
        model,
        wav[None],
        device=device,
        shifts=args.shifts,
        split=True,
        overlap=args.overlap,
        progress=True,
        num_workers=args.jobs,
        segment=args.segment,
    )[0]
    sources = sources * scale + offset
    if not th.isfinite(sources).all():
        raise ValueError("Demucs produced non-finite audio")
    vocals_index = model.sources.index("vocals")
    vocals = sources[vocals_index].detach().cpu().transpose(0, 1).numpy()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output, vocals, model.samplerate, subtype="PCM_16")
    print(f"Saved vocals to: {args.output}")


if __name__ == "__main__":
    main()
