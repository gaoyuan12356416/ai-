// GPU source fitting. Inputs are immutable 8-bit Y/U/V planes; output RGBA.
// Pixel-center coordinates and a normalized Lanczos-3 kernel preserve the
// existing cover/contain geometry. Padding remains transparent for main.
__constant sampler_t point_edge =
    CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP_TO_EDGE | CLK_FILTER_NEAREST;

float sinc3(float x)
{
    x = fabs(x);
    if (x < 0.00001f) return 1.0f;
    if (x >= 3.0f) return 0.0f;
    float t = 3.14159265358979323846f * x;
    return (sin(t) / t) * (sin(t / 3.0f) / (t / 3.0f));
}

float sample_plane(__read_only image2d_t plane, float2 position, float2 ratio)
{
    float2 down = fmax(ratio, (float2)(1.0f));
    int2 first = convert_int2(floor(position - 3.0f * down)) + (int2)(1);
    int2 last = convert_int2(ceil(position + 3.0f * down));
    float sum = 0.0f, weight = 0.0f;
    for (int y = first.y; y < last.y; ++y) {
        float wy = sinc3((position.y - (float)y) / down.y);
        for (int x = first.x; x < last.x; ++x) {
            float w = wy * sinc3((position.x - (float)x) / down.x);
            sum += w * read_imagef(plane, point_edge, (int2)(x, y)).x;
            weight += w;
        }
    }
    return sum / fmax(weight, 0.00001f);
}

float4 fit_yuv(__read_only image2d_t yplane, __read_only image2d_t uplane,
               __read_only image2d_t vplane, int2 output, int cover)
{
    int iw = get_image_width(yplane), ih = get_image_height(yplane);
    float factor = cover ? fmax(720.0f / iw, 1280.0f / ih)
                         : fmin(720.0f / iw, 1280.0f / ih);
    int fw = max(1, (int)floor(iw * factor + 0.5f));
    int fh = max(1, (int)floor(ih * factor + 0.5f));
    int2 offset = cover ? (int2)((fw - 720) / 2, (fh - 1280) / 2)
                        : (int2)(-((720 - fw) / 2), -((1280 - fh) / 2));
    int2 pixel = output + offset;
    if (!cover && (pixel.x < 0 || pixel.y < 0 || pixel.x >= fw || pixel.y >= fh))
        return (float4)(0.0f);
    float2 ratio = (float2)((float)iw / fw, (float)ih / fh);
    float2 location = (convert_float2(pixel) + 0.5f) * ratio - 0.5f;
    float y = sample_plane(yplane, location, ratio);
    float2 chroma = (location - (float2)(GPU_CHROMA_X, GPU_CHROMA_Y)) * 0.5f;
    float u = sample_plane(uplane, chroma, ratio * 0.5f);
    float v = sample_plane(vplane, chroma, ratio * 0.5f);
#if GPU_FULL_RANGE
    u -= 128.0f / 255.0f;
    v -= 128.0f / 255.0f;
#else
    y = (y * 255.0f - 16.0f) / 219.0f;
    u = (u * 255.0f - 128.0f) / 224.0f;
    v = (v * 255.0f - 128.0f) / 224.0f;
#endif
#if GPU_BT709
    float3 rgb = (float3)(y + 1.5748f * v,
                         y - 0.187324f * u - 0.468124f * v,
                         y + 1.8556f * u);
#else
    float3 rgb = (float3)(y + 1.402f * v,
                         y - 0.344136f * u - 0.714136f * v,
                         y + 1.772f * u);
#endif
    return (float4)(clamp(rgb, 0.0f, 1.0f), 1.0f);
}

__kernel void fit_yuv_cover(__write_only image2d_t destination, unsigned int frame,
    __read_only image2d_t rgba_clock, __read_only image2d_t y,
    __read_only image2d_t u, __read_only image2d_t v)
{
    (void)frame; (void)rgba_clock;
    int2 pixel = (int2)(get_global_id(0), get_global_id(1));
    if (pixel.x < 720 && pixel.y < 1280)
        write_imagef(destination, pixel, fit_yuv(y, u, v, pixel, 1));
}

__kernel void fit_yuv_contain(__write_only image2d_t destination, unsigned int frame,
    __read_only image2d_t rgba_clock, __read_only image2d_t y,
    __read_only image2d_t u, __read_only image2d_t v)
{
    (void)frame; (void)rgba_clock;
    int2 pixel = (int2)(get_global_id(0), get_global_id(1));
    if (pixel.x < 720 && pixel.y < 1280)
        write_imagef(destination, pixel, fit_yuv(y, u, v, pixel, 0));
}
