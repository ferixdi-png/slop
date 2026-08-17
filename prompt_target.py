def lock_generation_target(package):
    """Final deterministic target settings after Gemini creates the semantic package."""
    package.block_3_video.model = "gemini-omni-flash-preview"
    package.block_3_video.aspect_ratio = "9:16"

    frame_prompt = package.block_1_frame0_prompt or ""
    if "9:16" not in frame_prompt:
        package.block_1_frame0_prompt = (
            "VERTICAL OUTPUT FORMAT: 9:16 smartphone frame.\n" + frame_prompt
        )
    return package
