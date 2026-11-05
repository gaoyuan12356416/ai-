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

__device__ float sample_precomputed(const unsigned char* p,int pitch,int step,
    int width,int height,float x,float y,float rx,float ry,int bits,
    int ox,int oy,int plane,const int* mx,const int* my,
    const float* wx,const float* wy,int taps) {
    if(rx<=1.0f && ry<=1.0f && x==floorf(x) && y==floorf(y))
        return plane_pixel(p,pitch,step,width,height,(int)x,(int)y,bits);
    int ax=plane*720+ox,ay=plane*1280+oy;
    int x0=mx[ax*2],x1=mx[ax*2+1],y0=my[ay*2],y1=my[ay*2+1];
    float sum=0.0f,weight=0.0f;
    for(int yy=y0;yy<y1;++yy) {
        float yw=wy[ay*taps+yy-y0];
        for(int xx=x0;xx<x1;++xx) {
            float w=yw*wx[ax*taps+xx-x0];
            sum+=w*plane_pixel(p,pitch,step,width,height,xx,yy,bits);
            weight+=w;
        }
    }
    return sum/fmaxf(weight,0.00001f);
}
