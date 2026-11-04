// CUDA implementation of the existing OpenCL fitting and composition stages.
// Decode surfaces and the generated RGBA surface remain in device memory.
#include <cuda_runtime.h>

__device__ float limit(float x) { return fminf(1.0f, fmaxf(0.0f, x)); }
__device__ unsigned char unorm(float x) { return (unsigned char)__float2uint_rn(limit(x) * 255.0f); }
__device__ float sinc3(float x) {
    x = fabsf(x);
    if (x < 0.00001f) return 1.0f;
    if (x >= 3.0f) return 0.0f;
    float t = 3.14159265358979323846f * x;
    return (sinf(t) / t) * (sinf(t / 3.0f) / (t / 3.0f));
}
__device__ float plane_pixel(const unsigned char* p, int pitch, int step,
                            int width, int height, int x, int y, int bits) {
    x = max(0, min(width - 1, x)); y = max(0, min(height - 1, y));
    if (bits > 8) {
        const unsigned short* row = (const unsigned short*)(p + (long long)y*pitch);
        return (row[x*step] >> (16-bits)) / (float)((1<<bits)-1);
    }
    return p[(long long)y * pitch + x * step] / 255.0f;
}
__device__ float sample_plane(const unsigned char* p, int pitch, int step,
                             int width, int height, float x, float y,
                             float rx, float ry, int bits) {
    if (rx <= 1.0f && ry <= 1.0f && x == floorf(x) && y == floorf(y))
        return plane_pixel(p, pitch, step, width, height, (int)x, (int)y, bits);
    float dx = fmaxf(rx, 1.0f), dy = fmaxf(ry, 1.0f);
    int x0 = (int)floorf(x - 3.0f * dx) + 1;
    int y0 = (int)floorf(y - 3.0f * dy) + 1;
    int x1 = (int)ceilf(x + 3.0f * dx), y1 = (int)ceilf(y + 3.0f * dy);
    float sum = 0.0f, weight = 0.0f;
    for (int yy = y0; yy < y1; ++yy) {
        float wy = sinc3((y - yy) / dy);
        for (int xx = x0; xx < x1; ++xx) {
            float w = wy * sinc3((x - xx) / dx);
            sum += w * plane_pixel(p, pitch, step, width, height, xx, yy, bits);
            weight += w;
        }
    }
    return sum / fmaxf(weight, 0.00001f);
}

extern "C" __global__ void fit_nv12(
    const unsigned char* yp, const unsigned char* uvp, int ypitch, int uvpitch,
    int iw, int ih, uchar4* out, int cover, int full_range, int bt709,
    float chroma_x, float chroma_y, float sar, int bits, int legacy_fit) {
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= 720 || y >= 1280) return;
    // ffmpeg scale first fits storage dimensions; its following setsar=1
    // intentionally normalizes non-square pixels. sar remains an explicit
    // argument so admission can prove the selected geometry contract.
    (void)sar;
    float factor = cover ? fmaxf(720.0f / iw, 1280.0f / ih)
                         : fminf(720.0f / iw, 1280.0f / ih);
    int fw = max(1, (int)floorf(iw * factor + 0.5f));
    int fh = max(1, (int)floorf(ih * factor + 0.5f));
    int px = x + (cover ? (fw - 720) / 2 : -((720 - fw) / 2));
    int py = y + (cover ? (fh - 1280) / 2 : -((1280 - fh) / 2));
    if (!cover && (px < 0 || py < 0 || px >= fw || py >= fh)) {
        out[y * 720 + x] = make_uchar4(0, 0, 0, 0); return;
    }
    float rx = (float)iw / fw, ry = (float)ih / fh;
    float lx = (px + 0.5f) * rx - 0.5f, ly = (py + 0.5f) * ry - 0.5f;
    float yy = sample_plane(yp, ypitch, 1, iw, ih, lx, ly, rx, ry, bits);
    float cx = (lx - chroma_x) * 0.5f, cy = (ly - chroma_y) * 0.5f;
    float crx=rx*.5f,cry=ry*.5f;
    if(legacy_fit) {
        // FFmpeg scale defaults to center input siting. Packed RGBA output
        // without full_chroma_int reuses each half-width chroma sample twice.
        crx=(iw/2.0f)/((fw+1)/2);
        cry=(ih/2.0f)/fh;
        cx=((px/2)+.25f)*crx-.5f;
        cy=(py+.5f)*cry-.5f;
    }
    float u = sample_plane(uvp, uvpitch, 2, iw / 2, ih / 2, cx, cy, crx, cry, bits);
    float v = sample_plane(uvp + (bits>8?2:1), uvpitch, 2, iw / 2, ih / 2, cx, cy, crx, cry, bits);
    float maxval=(float)((1<<bits)-1), scale=(float)(1<<(bits-8));
    if (full_range) { u -= 128.0f*scale/maxval; v -= 128.0f*scale/maxval; }
    else { yy = (yy * maxval - 16.0f*scale) / (219.0f*scale);
           u = (u * maxval - 128.0f*scale) / (224.0f*scale);
           v = (v * maxval - 128.0f*scale) / (224.0f*scale); }
    float r = yy + (bt709==2 ? 1.4746f : (bt709 ? 1.5748f : 1.402f)) * v;
    float g = yy - (bt709==2 ? 0.164553f : (bt709 ? 0.187324f : 0.344136f)) * u
                 - (bt709==2 ? 0.571353f : (bt709 ? 0.468124f : 0.714136f)) * v;
    float b = yy + (bt709==2 ? 1.8814f : (bt709 ? 1.8556f : 1.772f)) * u;
    if(bt709>=3) {
        float kr=bt709==3?.212f:.30f,kb=bt709==3?.087f:.11f;
        r=yy+2*(1-kr)*v;b=yy+2*(1-kb)*u;
        g=yy-2*kb*(1-kb)/(1-kr-kb)*u-2*kr*(1-kr)/(1-kr-kb)*v;
    }
    out[y * 720 + x] = make_uchar4(unorm(r), unorm(g), unorm(b), 255);
}

__device__ float4 pixel_rgba(const uchar4* p, int w, int h, int x, int y, int edge) {
    if (!edge && (x < 0 || y < 0 || x >= w || y >= h)) return make_float4(0,0,0,0);
    x = max(0, min(w - 1, x)); y = max(0, min(h - 1, y));
    uchar4 v = p[(long long)y * w + x];
    return make_float4(v.x / 255.0f, v.y / 255.0f, v.z / 255.0f, v.w / 255.0f);
}
__device__ float4 linear_rgba(const uchar4* p, int w, int h, float u, float v, int edge) {
    float x = u * w - 0.5f, y = v * h - 0.5f;
    int ix = (int)floorf(x), iy = (int)floorf(y);
    float fx = x - ix, fy = y - iy;
    float4 a = pixel_rgba(p,w,h,ix,iy,edge), b = pixel_rgba(p,w,h,ix+1,iy,edge);
    float4 c = pixel_rgba(p,w,h,ix,iy+1,edge), d = pixel_rgba(p,w,h,ix+1,iy+1,edge);
    float4 z;
    z.x = ((1-fx)*a.x+fx*b.x)*(1-fy)+((1-fx)*c.x+fx*d.x)*fy;
    z.y = ((1-fx)*a.y+fx*b.y)*(1-fy)+((1-fx)*c.y+fx*d.y)*fy;
    z.z = ((1-fx)*a.z+fx*b.z)*(1-fy)+((1-fx)*c.z+fx*d.z)*fy;
    z.w = ((1-fx)*a.w+fx*b.w)*(1-fy)+((1-fx)*c.w+fx*d.w)*fy;
    return z;
}
__device__ float4 over(float4 bottom, float4 top) {
    float a = limit(top.w);
    return make_float4(limit(top.x*a+bottom.x*(1-a)),
                       limit(top.y*a+bottom.y*(1-a)),
                       limit(top.z*a+bottom.z*(1-a)),1.0f);
}
extern "C" __global__ void compose_rgba(
    const uchar4* source, const uchar4* main,
    const uchar4* border, int bw, int bh,
    const uchar4* opacity, int ow, int oh,
    const uchar4* corners, int cw, int ch,
    const uchar4* tint, int tw, int th,
    uchar4* out, int mw, int mh, float angle, float tint_alpha,
    float source_alpha, float source_scale) {
    int x=blockIdx.x*blockDim.x+threadIdx.x, y=blockIdx.y*blockDim.y+threadIdx.y;
    if (x>=720 || y>=1280) return;
    float u=(x+0.5f)/720.0f, v=(y+0.5f)/1280.0f;
    float4 value=linear_rgba(source,720,1280,u,v,1); value.w=1.0f;
    float cx=u*720.0f-360.0f, cy=v*1280.0f-640.0f;
    float cosine=cosf(angle), sine=sinf(angle);
    float sx=mw*0.5f+cx*cosine+cy*sine;
    float sy=mh*0.5f-cx*sine+cy*cosine;
    float4 foreground=(sx<0 || sy<0 || sx>=mw || sy>=mh)
        ? make_float4(0,0,0,0) : linear_rgba(main,720,1280,sx/mw,sy/mh,0);
    value=over(value,foreground);
    float4 tp=linear_rgba(tint,tw,th,u,v,0); tp.w*=tint_alpha;
    value=over(value,tp);
    value=over(value,linear_rgba(opacity,ow,oh,u,v,0));
    value=over(value,linear_rgba(border,bw,bh,u,v,0));
    value=over(value,linear_rgba(corners,cw,ch,u,v,0));
    if (source_alpha>0.0f) {
        float4 ghost=linear_rgba(source,720,1280,(u-0.5f)/source_scale+0.5f,
                                (v-0.5f)/source_scale+0.5f,1);
        ghost.w=source_alpha; value=over(value,ghost);
    }
    out[y*720+x]=make_uchar4(unorm(value.x),unorm(value.y),unorm(value.z),255);
}

// FFmpeg describes component storage; only these few integers cross the host.
// The actual decoded plane bytes are read and converted exclusively on CUDA.
__device__ float component_at(const unsigned long long* pointers,const int* meta,
                             int component,int x,int y,int big_endian,int float_data) {
    const int* m=meta+component*7;
    x=max(0,min(m[5]-1,x)); y=max(0,min(m[6]-1,y));
    const unsigned char* p=(const unsigned char*)pointers[component]+(long long)y*m[0]+x*m[1]+m[2];
    int bytes=(m[3]+m[4]+7)/8;
    unsigned long long value=0;
    for(int i=0;i<bytes;++i) value|=(unsigned long long)p[i] << (8*(big_endian?bytes-1-i:i));
    if(float_data) {
        if(m[4]==32) return __uint_as_float((unsigned int)value);
        unsigned int e=(value>>10)&31,sign=(value&32768)?1:0,mant=value&1023;
        float v=e==0?ldexpf((float)mant,-24):e==31?__uint_as_float(0x7f800000|(mant<<13)):
                ldexpf(1.0f+mant/1024.0f,(int)e-15);
        return sign?-v:v;
    }
    unsigned long long mask=(1ULL<<m[4])-1;
    return ((value>>m[3])&mask)/(float)mask;
}
__device__ float component_sample(const unsigned long long* pointers,const int* meta,
    int c,float x,float y,float rx,float ry,int be,int float_data) {
    if(rx<=1 && ry<=1 && x==floorf(x) && y==floorf(y))
        return component_at(pointers,meta,c,(int)x,(int)y,be,float_data);
    float dx=fmaxf(rx,1.0f),dy=fmaxf(ry,1.0f),sum=0,weight=0;
    for(int yy=(int)floorf(y-3*dy)+1;yy<(int)ceilf(y+3*dy);++yy) {
        float wy=sinc3((y-yy)/dy);
        for(int xx=(int)floorf(x-3*dx)+1;xx<(int)ceilf(x+3*dx);++xx) {
            float w=wy*sinc3((x-xx)/dx);
            sum+=w*component_at(pointers,meta,c,xx,yy,be,float_data);weight+=w;
        }
    }
    return sum/fmaxf(weight,0.00001f);
}
extern "C" __global__ void fit_components(const unsigned long long* pointers,
    const int* meta,int count,int iw,int ih,int rgb,int big_endian,int float_data,
    uchar4* output,int cover,int full_range,int matrix,int center_chroma,int top_chroma,int legacy_fit) {
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=720 || y>=1280) return;
    float factor=cover?fmaxf(720.0f/iw,1280.0f/ih):fminf(720.0f/iw,1280.0f/ih);
    int fw=max(1,(int)floorf(iw*factor+.5f)),fh=max(1,(int)floorf(ih*factor+.5f));
    int px=x+(cover?(fw-720)/2:-((720-fw)/2)),py=y+(cover?(fh-1280)/2:-((1280-fh)/2));
    if(!cover && (px<0 || py<0 || px>=fw || py>=fh)) {
        output[y*720+x]=make_uchar4(0,0,0,0);return;
    }
    float rx=(float)iw/fw,ry=(float)ih/fh,lx=(px+.5f)*rx-.5f,ly=(py+.5f)*ry-.5f;
    float c[4]={0,0,0,1};
    for(int i=0;i<count;++i) {
        float sx=(float)meta[i*7+5]/iw,sy=(float)meta[i*7+6]/ih;
        float ox=(!rgb && i>0 && i<3 && center_chroma)?(1/sx-1)*.5f:0;
        float oy=(!rgb && i>0 && i<3 && !top_chroma)?(1/sy-1)*.5f:0;
        if(!rgb && i>0 && i<3 && legacy_fit) {
            float crx=(float)meta[i*7+5]/((fw+1)/2),cry=(float)meta[i*7+6]/fh;
            c[i]=component_sample(pointers,meta,i,((px/2)+.25f)*crx-.5f,
                                  (py+.5f)*cry-.5f,crx,cry,big_endian,float_data);
        } else {
            c[i]=component_sample(pointers,meta,i,(lx-ox)*sx,(ly-oy)*sy,rx*sx,ry*sy,big_endian,float_data);
        }
    }
    float r=c[0],g=c[1],b=c[2];
    if(!rgb) {
        int bits=meta[4];float maxval=(float)((1ULL<<bits)-1),scale=(float)(1ULL<<(bits-8));
        float yy=c[0],u=0,v=0;
        if(count>=3) {
            if(full_range) { u=c[1]-128*scale/maxval;v=c[2]-128*scale/maxval; }
            else { u=(c[1]*maxval-128*scale)/(224*scale);v=(c[2]*maxval-128*scale)/(224*scale); }
        }
        if(!full_range) yy=(yy*maxval-16*scale)/(219*scale);
        float kr=matrix==1?.2126f:matrix==2?.2627f:matrix==3?.212f:matrix==4?.30f:.299f;
        float kb=matrix==1?.0722f:matrix==2?.0593f:matrix==3?.087f:matrix==4?.11f:.114f;
        r=yy+2*(1-kr)*v;b=yy+2*(1-kb)*u;
        g=yy-2*kb*(1-kb)/(1-kr-kb)*u-2*kr*(1-kr)/(1-kr-kb)*v;
    }
    output[y*720+x]=make_uchar4(unorm(r),unorm(g),unorm(b),unorm(c[3]));
}
