// Device-only conversion/composition. Geometry matches random_overlay_v2.cl.
// Runtime prepends the same validated SCENE_* constants as the OpenCL kernel.
#include <cuda_runtime.h>

__device__ float clip(float x) { return fminf(1.0f, fmaxf(0.0f, x)); }
__device__ unsigned char byte(float x) { return (unsigned char)__float2int_rn(clip(x) * 255.0f); }
__device__ float4 pixel(const uchar4* p, int w, int h, int x, int y) {
    uchar4 v = p[min(max(y,0),h-1)*w + min(max(x,0),w-1)];
    return make_float4(v.x/255.f,v.y/255.f,v.z/255.f,v.w/255.f);
}
__device__ float4 mix4(float4 a, float4 b, float t) {
    return make_float4(a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t,
                      a.z+(b.z-a.z)*t,a.w+(b.w-a.w)*t);
}
__device__ float4 sample(const uchar4* p, int w, int h, float u, float v) {
    float x=u*w-0.5f,y=v*h-0.5f;int ix=(int)floorf(x),iy=(int)floorf(y);
    return mix4(mix4(pixel(p,w,h,ix,iy),pixel(p,w,h,ix+1,iy),x-ix),
                mix4(pixel(p,w,h,ix,iy+1),pixel(p,w,h,ix+1,iy+1),x-ix),y-iy);
}
__device__ float4 cover(const uchar4* p,int w,int h,float u,float v) {
    float aspect=(float)w/h,canvas=(float)SCENE_WIDTH/SCENE_HEIGHT;
    if(aspect>canvas) u=(1.f-canvas/aspect)*.5f+u*canvas/aspect;
    else v=(1.f-aspect/canvas)*.5f+v*aspect/canvas;
    float4 result=sample(p,w,h,u,v);result.w=1;return result;
}
__device__ float4 over(float4 a,float4 b) {
    float t=clip(b.w);
    return make_float4(clip(b.x*t+a.x*(1-t)),clip(b.y*t+a.y*(1-t)),
                      clip(b.z*t+a.z*(1-t)),1.f);
}
__device__ float chroma(const unsigned char* uv,int pitch,int w,int h,
                        float x,float y,int channel) {
    // Left-sited 4:2:0: horizontal sample at 2*x, vertical center at 2*y+.5.
    float fx=x*.5f,fy=(y-.5f)*.5f;
    int ix=(int)floorf(fx),iy=(int)floorf(fy);float dx=fx-ix,dy=fy-iy;
    int x0=min(max(ix,0),w/2-1)*2+channel,x1=min(max(ix+1,0),w/2-1)*2+channel;
    int y0=min(max(iy,0),h/2-1)*pitch,y1=min(max(iy+1,0),h/2-1)*pitch;
    return (uv[y0+x0]*(1-dx)+uv[y0+x1]*dx)*(1-dy)
         + (uv[y1+x0]*(1-dx)+uv[y1+x1]*dx)*dy;
}
extern "C" __global__ void nv12_rgba(const unsigned char* src,uchar4* dst,
                                    int w,int h,int pitch) {
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=w||y>=h)return;
    const unsigned char* uv=src+pitch*h;
    float l=(src[y*pitch+x]-16.f)/219.f;
    float u=(chroma(uv,pitch,w,h,(float)x,(float)y,0)-128.f)/224.f;
    float v=(chroma(uv,pitch,w,h,(float)x,(float)y,1)-128.f)/224.f;
    dst[y*w+x]=make_uchar4(byte(l+1.5748f*v),byte(l-.187324f*u-.468124f*v),
                           byte(l+1.8556f*u),255);
}
extern "C" __global__ void compose(const uchar4* src,int w,int h,
    const uchar4* border,const uchar4* opacity,const uchar4* corners,const uchar4* tint,
    uchar4* dst) {
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=SCENE_WIDTH||y>=SCENE_HEIGHT)return;
    float u=(x+.5f)/SCENE_WIDTH,v=(y+.5f)/SCENE_HEIGHT;
    float4 value=cover(src,w,h,u,v);
    float cx=x+.5f-SCENE_WIDTH*.5f,cy=y+.5f-SCENE_HEIGHT*.5f;
    float cs=cosf(SCENE_ROTATION_RADIANS),sn=sinf(SCENE_ROTATION_RADIANS);
    float mx=SCENE_MAIN_WIDTH*.5f+cx*cs+cy*sn;
    float my=SCENE_MAIN_HEIGHT*.5f-cx*sn+cy*cs;
    if(mx>=0&&my>=0&&mx<SCENE_MAIN_WIDTH&&my<SCENE_MAIN_HEIGHT)
        value=over(value,cover(src,w,h,mx/SCENE_MAIN_WIDTH,my/SCENE_MAIN_HEIGHT));
    int i=y*SCENE_WIDTH+x;
    float4 t=pixel(tint,SCENE_WIDTH,SCENE_HEIGHT,x,y);t.w*=SCENE_TINT_OPACITY;
    value=over(value,t);
    value=over(value,pixel(opacity,SCENE_WIDTH,SCENE_HEIGHT,x,y));
    value=over(value,pixel(border,SCENE_WIDTH,SCENE_HEIGHT,x,y));
    value=over(value,pixel(corners,SCENE_WIDTH,SCENE_HEIGHT,x,y));
    dst[i]=make_uchar4(byte(value.x),byte(value.y),byte(value.z),255);
}
extern "C" __global__ void rgba_nv12(const uchar4* src,unsigned char* dst) {
    int x=blockIdx.x*blockDim.x+threadIdx.x,y=blockIdx.y*blockDim.y+threadIdx.y;
    if(x>=SCENE_WIDTH||y>=SCENE_HEIGHT)return;
    int i=y*SCENE_WIDTH+x;uchar4 a=src[i];
    float l=.2126f*a.x+.7152f*a.y+.0722f*a.z;
    dst[i]=(unsigned char)__float2int_rn(16.f+l*219.f/255.f);
    if((x&1)||(y&1))return;
    uchar4 b=src[i+1],c=src[i+SCENE_WIDTH],d=src[i+SCENE_WIDTH+1];
    float r=(a.x+b.x+c.x+d.x)*.25f,g=(a.y+b.y+c.y+d.y)*.25f,bl=(a.z+b.z+c.z+d.z)*.25f;
    l=.2126f*r+.7152f*g+.0722f*bl;
    int j=SCENE_WIDTH*SCENE_HEIGHT+(y/2)*SCENE_WIDTH+x;
    dst[j]=(unsigned char)__float2int_rn(128.f+(bl-l)/1.8556f*224.f/255.f);
    dst[j+1]=(unsigned char)__float2int_rn(128.f+(r-l)/1.5748f*224.f/255.f);
}
