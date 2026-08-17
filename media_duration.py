import os
import struct
import subprocess


def _ffprobe_duration(path):
    try:
        p = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if p.returncode == 0 and p.stdout.strip():
            value = float(p.stdout.strip())
            if value > 0:
                return value
    except Exception:
        pass
    return 0.0


def _read_u32(f):
    data = f.read(4)
    return struct.unpack(">I", data)[0] if len(data) == 4 else None


def _read_u64(f):
    data = f.read(8)
    return struct.unpack(">Q", data)[0] if len(data) == 8 else None


def _mp4_mvhd_duration(path):
    """Best-effort ISO-BMFF mvhd parser; used when ffprobe is not installed."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            offset = 0
            while offset + 8 <= size:
                f.seek(offset)
                box_size = _read_u32(f)
                box_type = f.read(4)
                header = 8
                if box_size == 1:
                    box_size = _read_u64(f)
                    header = 16
                elif box_size == 0:
                    box_size = size - offset
                if not box_size or box_size < header:
                    break

                if box_type == b"moov":
                    moov_end = min(size, offset + box_size)
                    child = offset + header
                    while child + 8 <= moov_end:
                        f.seek(child)
                        child_size = _read_u32(f)
                        child_type = f.read(4)
                        child_header = 8
                        if child_size == 1:
                            child_size = _read_u64(f)
                            child_header = 16
                        elif child_size == 0:
                            child_size = moov_end - child
                        if not child_size or child_size < child_header:
                            break
                        if child_type == b"mvhd":
                            f.seek(child + child_header)
                            version_b = f.read(1)
                            if not version_b:
                                return 0.0
                            version = version_b[0]
                            f.read(3)  # flags
                            if version == 1:
                                f.read(16)  # creation + modification
                                timescale = _read_u32(f)
                                duration = _read_u64(f)
                            else:
                                f.read(8)  # creation + modification
                                timescale = _read_u32(f)
                                duration = _read_u32(f)
                            if timescale and duration is not None:
                                return float(duration) / float(timescale)
                            return 0.0
                        child += child_size
                    return 0.0
                offset += box_size
    except Exception:
        return 0.0
    return 0.0


def measure_video_duration(path, fallback=0.0):
    value = _ffprobe_duration(path) or _mp4_mvhd_duration(path)
    if value > 0:
        return round(value, 3)
    try:
        return round(float(fallback or 0), 3)
    except Exception:
        return 0.0
