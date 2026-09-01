// FFmpeg program_opencl fused random-overlay compositor.
// Runtime prepends validated SCENE_* constants; this template never receives
// request text or filesystem data.

__constant sampler_t linear_edge =
    CLK_NORMALIZED_COORDS_TRUE | CLK_ADDRESS_CLAMP_TO_EDGE | CLK_FILTER_LINEAR;
__constant sampler_t linear_clear =
    CLK_NORMALIZED_COORDS_TRUE | CLK_ADDRESS_CLAMP | CLK_FILTER_LINEAR;

float4 over(float4 bottom, float4 top)
{
    float alpha = clamp(top.w, 0.0f, 1.0f);
    float3 rgb = top.xyz * alpha + bottom.xyz * (1.0f - alpha);
    return (float4)(clamp(rgb, 0.0f, 1.0f), 1.0f);
}

float4 sample_cover(__read_only image2d_t source, float2 canvas_uv)
{
    float source_aspect = (float)get_image_width(source) / (float)get_image_height(source);
    float canvas_aspect = (float)SCENE_WIDTH / (float)SCENE_HEIGHT;
    float2 uv = canvas_uv;
    if (source_aspect > canvas_aspect) {
        float visible = canvas_aspect / source_aspect;
        uv.x = (1.0f - visible) * 0.5f + uv.x * visible;
    } else {
        float visible = source_aspect / canvas_aspect;
        uv.y = (1.0f - visible) * 0.5f + uv.y * visible;
    }
    float4 pixel = read_imagef(source, linear_edge, uv);
    pixel.w = 1.0f;
    return pixel;
}

float4 sample_main(__read_only image2d_t source, float2 canvas_uv)
{
    // Clean profile: inverse-map each output pixel directly into the centered
    // scaled main plane. There is no intermediate rotate canvas whose malformed
    // extent can clip the frame into horizontal bands.
    float2 centered = canvas_uv *
        (float2)((float)SCENE_WIDTH, (float)SCENE_HEIGHT) -
        (float2)((float)SCENE_WIDTH * 0.5f, (float)SCENE_HEIGHT * 0.5f);
    float cosine = cos(SCENE_ROTATION_RADIANS);
    float sine = sin(SCENE_ROTATION_RADIANS);
    float2 scaled = (float2)(
        (float)SCENE_MAIN_WIDTH * 0.5f +
            centered.x * cosine + centered.y * sine,
        (float)SCENE_MAIN_HEIGHT * 0.5f -
            centered.x * sine + centered.y * cosine
    );
    if (scaled.x < 0.0f || scaled.x >= (float)SCENE_MAIN_WIDTH ||
        scaled.y < 0.0f || scaled.y >= (float)SCENE_MAIN_HEIGHT)
        return (float4)(0.0f, 0.0f, 0.0f, 0.0f);

    // Map the transparent contain/pad plane back to the original source.
    float2 fitted_uv = scaled /
        (float2)((float)SCENE_MAIN_WIDTH, (float)SCENE_MAIN_HEIGHT);
    float source_aspect = (float)get_image_width(source) / (float)get_image_height(source);
    float canvas_aspect = (float)SCENE_WIDTH / (float)SCENE_HEIGHT;
    float2 uv;
    if (source_aspect > canvas_aspect) {
        float height = canvas_aspect / source_aspect;
        uv = (float2)(fitted_uv.x, (fitted_uv.y - 0.5f) / height + 0.5f);
    } else {
        float width = source_aspect / canvas_aspect;
        uv = (float2)((fitted_uv.x - 0.5f) / width + 0.5f, fitted_uv.y);
    }
    if (uv.x < 0.0f || uv.x > 1.0f || uv.y < 0.0f || uv.y > 1.0f)
        return (float4)(0.0f, 0.0f, 0.0f, 0.0f);
    float4 pixel = read_imagef(source, linear_clear, uv);
    pixel.w = 1.0f;
    return pixel;
}

__kernel void compose_random_overlay_v2(
    __write_only image2d_t destination,
    unsigned int frame_index,
    __read_only image2d_t source,
    __read_only image2d_t border,
    __read_only image2d_t opacity_video,
    __read_only image2d_t corners,
    __read_only image2d_t tint)
{
    (void)frame_index;
    int2 coordinate = (int2)(get_global_id(0), get_global_id(1));
    if (coordinate.x >= SCENE_WIDTH || coordinate.y >= SCENE_HEIGHT)
        return;
    float2 uv = ((convert_float2(coordinate)) + (float2)(0.5f, 0.5f)) /
                (float2)((float)SCENE_WIDTH, (float)SCENE_HEIGHT);
    float4 value = sample_cover(source, uv);
    value = over(value, sample_main(source, uv));
    float4 tint_pixel = read_imagef(tint, linear_clear, uv);
    tint_pixel.w *= SCENE_TINT_OPACITY;
    value = over(value, tint_pixel);
    value = over(value, read_imagef(opacity_video, linear_clear, uv));
    value = over(value, read_imagef(border, linear_clear, uv));
    value = over(value, read_imagef(corners, linear_clear, uv));
    write_imagef(destination, coordinate, clamp(value, 0.0f, 1.0f));
}
