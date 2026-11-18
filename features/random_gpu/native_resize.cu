// Fixed source geometry: compute the identical sinc3 coefficients once per
// axis. Retain the original 2-D accumulation order (including normalization).
extern "C" __global__ void fit_weights(int iw,int ih,int cover,int legacy,
    float chroma_x,float chroma_y,int axis,int taps,int* meta,float* weights) {
    int i=blockIdx.x*blockDim.x+threadIdx.x, plane=blockIdx.y;
    int n=axis?1280:720;
    if(i>=n)return;
    float factor=cover?fmaxf(720.0f/iw,1280.0f/ih):fminf(720.0f/iw,1280.0f/ih);
    int fw=max(1,(int)floorf(iw*factor+.5f)),fh=max(1,(int)floorf(ih*factor+.5f));
    int p=axis?(i+(cover?(fh-1280)/2:-((1280-fh)/2))):
               (i+(cover?(fw-720)/2:-((720-fw)/2)));
    float r=axis?(float)ih/fh:(float)iw/fw;
    float c=(p+.5f)*r-.5f;
    if(plane) {
        if(legacy) {
            r=axis?(ih/2.0f)/fh:(iw/2.0f)/((fw+1)/2);
            c=axis?(p+.5f)*r-.5f:((p/2)+.25f)*r-.5f;
        } else {
            c=(c-(axis?chroma_y:chroma_x))*.5f;
            r*=.5f;
        }
    }
    float d=fmaxf(r,1.0f);
    int start=(int)floorf(c-3.0f*d)+1,end=(int)ceilf(c+3.0f*d);
    int at=plane*n+i;
    meta[at*2]=start;meta[at*2+1]=end;
    for(int k=0;k<end-start && k<taps;++k)
        weights[at*taps+k]=sinc3((c-(start+k))/d);
}

// The normalization depends only on geometry. Preserve the exact original
// nested-loop order and float operations, but evaluate it once per plan.
extern "C" __global__ void fit_normalizers(const int* mx,const int* my,
    const float* wx,const float* wy,int taps,float* normalizers) {
    int ox=blockIdx.x*blockDim.x+threadIdx.x;
    int oy=blockIdx.y*blockDim.y+threadIdx.y,plane=blockIdx.z;
    if(ox>=720 || oy>=1280)return;
    int ax=plane*720+ox,ay=plane*1280+oy;
    int x0=mx[ax*2],x1=mx[ax*2+1],y0=my[ay*2],y1=my[ay*2+1];
    float weight=0.0f;
    for(int yy=y0;yy<y1;++yy) {
        float yw=wy[ay*taps+yy-y0];
        for(int xx=x0;xx<x1;++xx) {
            // The reference shares this rounded product with the pixel sum.
            // Do not contract its multiplication into the normalization add.
            float w=__fmul_rn(yw,wx[ax*taps+xx-x0]);
            weight+=w;
        }
    }
    normalizers[(plane*1280+oy)*720+ox]=fmaxf(weight,0.00001f);
}

__device__ float sample_precomputed(const unsigned char* p,int pitch,int step,
    int width,int height,float x,float y,float rx,float ry,int bits,
    int ox,int oy,int plane,const int* mx,const int* my,
    const float* wx,const float* wy,const float* normalizers,int taps) {
    if(rx<=1.0f && ry<=1.0f && x==floorf(x) && y==floorf(y))
        return plane_pixel(p,pitch,step,width,height,(int)x,(int)y,bits);
    int ax=plane*720+ox,ay=plane*1280+oy;
    int x0=mx[ax*2],x1=mx[ax*2+1],y0=my[ay*2],y1=my[ay*2+1];
    float sum=0.0f;
    for(int yy=y0;yy<y1;++yy) {
        float yw=wy[ay*taps+yy-y0];
        for(int xx=x0;xx<x1;++xx) {
            float w=yw*wx[ax*taps+xx-x0];
            sum+=w*plane_pixel(p,pitch,step,width,height,xx,yy,bits);
        }
    }
    return sum/normalizers[(plane*1280+oy)*720+ox];
}

// U and V share coordinates and weights. Interleave their independent sums
// without reassociating either one or changing the source reads/rounding.
__device__ float2 sample_uv_precomputed(const unsigned char* p,int pitch,
    int width,int height,float x,float y,float rx,float ry,int bits,
    int ox,int oy,const int* mx,const int* my,
    const float* wx,const float* wy,const float* normalizers,int taps) {
    const unsigned char* vp=p+(bits>8?2:1);
    if(rx<=1.0f && ry<=1.0f && x==floorf(x) && y==floorf(y))
        return make_float2(plane_pixel(p,pitch,2,width,height,(int)x,(int)y,bits),
                           plane_pixel(vp,pitch,2,width,height,(int)x,(int)y,bits));
    int ax=720+ox,ay=1280+oy;
    int x0=mx[ax*2],x1=mx[ax*2+1],y0=my[ay*2],y1=my[ay*2+1];
    float u=0.0f,v=0.0f;
    for(int yy=y0;yy<y1;++yy) {
        float yw=wy[ay*taps+yy-y0];
        for(int xx=x0;xx<x1;++xx) {
            float w=yw*wx[ax*taps+xx-x0];
            u+=w*plane_pixel(p,pitch,2,width,height,xx,yy,bits);
            v+=w*plane_pixel(vp,pitch,2,width,height,xx,yy,bits);
        }
    }
    float weight=normalizers[(1280+oy)*720+ox];
    return make_float2(u/weight,v/weight);
}
