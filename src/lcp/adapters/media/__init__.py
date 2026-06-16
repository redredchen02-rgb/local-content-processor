"""Media I/O adapters: Pillow image normalization + ffprobe/ffmpeg probing.

All side effects (reading files off disk, spawning subprocesses) live here.
Pure threshold judgement is delegated to :mod:`lcp.core.rules.asset_rules`.
"""
