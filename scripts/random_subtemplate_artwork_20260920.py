"""Deterministic, edge-focused artwork for the September 20 catalog expansion.

Artwork is drawn at 360x640 and sampled at 720x1280 with Lanczos filtering.
Every animation is a four-second periodic function, without external images.
"""
from __future__ import annotations

import math
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 360, 640
TAU = math.tau
Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
U, V = X / (W - 1), Y / (H - 1)
EDGE_X = np.minimum(X, W - 1 - X)
EDGE_Y = np.minimum(Y, H - 1 - Y)

GROUPS = {
    "border": {
        "start": 9, "prefix": "B", "names": ["折扇金边", "邮票齿孔", "拱窗银框", "回纹细格", "贝壳蕾丝", "蜂巢侧栏", "交织缝线", "棱镜切面", "海波轮廓", "留白书签", "珍珠串边", "蔷薇藤蔓"],
        "descriptions": ["细金线围合，四角展开装饰折扇", "奶油邮票边与均匀的半圆齿孔", "四角哥特拱窗，细银线连接", "低饱和石青色连续回纹", "暖白贝壳扇形与花边小珠", "冰青蜂巢沿两侧排列", "陶土色交叉绣线与双针脚", "低透明多边形折面，点亮四角", "靛蓝多重海浪细线", "不对称墨绿书刊线与角部色签", "香槟珍珠链与小型中心宝石", "粉金玫瑰与细叶藤蔓"],
    },
    "opacity_video": {
        "start": 11, "prefix": "O", "names": ["轻雪漫舞", "银杏秋语", "海光游弋", "樱瓣轻落", "萤火微轨", "绸带流光", "霜雾晶花", "透光泡泡", "棱彩碎片", "环形涟漪"],
        "descriptions": ["大小雪点沿侧边轻落，雪晶缓缓旋转", "扇形银杏叶在两侧翻转飘落", "青蓝焦散光纹沿边缘周期游动", "不同姿态的半透明花瓣顺风飘落", "暖绿色萤火点沿弧形轨迹游走", "珊瑚和珍珠色细绸带往复摆动", "冷色薄雾中浮现六角霜花", "透明彩色气泡缓缓向上漂浮", "三角与菱形彩片在两侧翻转", "边缘水纹同心环逐渐扩散消隐"],
    },
    "corners": {
        "start": 9, "prefix": "C", "names": ["折纸白鹤", "晨光雏菊", "蝶翼轻颤", "六角机芯", "轻音乐章", "月桂花环", "水晶垂饰", "暖灯轻摇", "航海罗盘", "纸翼远行", "丝带蝴结", "春日郁金"],
        "descriptions": ["浅蓝折纸鹤轻轻振翼", "暖白花瓣围绕金色花蕊慢转", "粉紫蝴蝶左右翅翼舒展", "两层六角线框反向转动", "成组音符在短五线谱上轻跳", "金绿色月桂枝沿角部舒展", "细链悬挂的切面水晶摆动", "暖红纸灯笼与流苏轻轻摇摆", "青金罗盘与方向针缓缓摆动", "薄荷纸飞机与虚线飞行轨迹", "柔粉缎带蝴蝶结随呼吸微动", "三枝郁金香与嫩叶摇曳"],
    },
    "tint": {
        "start": 13, "prefix": "T", "names": ["曙光放射", "海陆斜映", "珊瑚紫晕", "苔金回光", "暮蓝暖窗", "铜青交辉", "绯紫丝光", "瓷白虹彩"],
        "descriptions": ["左上杏金光心向右下玫瑰与蓝灰过渡", "青蓝与橘色沿对角线平滑交汇", "柔紫椭圆光心向珊瑚色四周扩散", "橄榄与浅金从不同方向交织", "冷蓝背景叠加偏右的暖色椭圆光域", "铜色与青色光从相对角交叉映射", "桃粉底色上叠加柔和斜向紫色光带", "低饱和瓷白底色上分布青粉金三个柔光域"],
    },
}


def blank():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def _rgba(rgb, alpha):
    result = np.empty((H, W, 4), dtype=np.uint8)
    result[:, :, :3] = np.clip(rgb, 0, 255)
    result[:, :, 3] = np.clip(alpha, 0, 255)
    return Image.fromarray(result)


def _star(d, x, y, radius, color, rotation=0, points=4, inner=.27, width=1):
    vertices = [(x + (radius if n % 2 == 0 else radius * inner) * math.cos(rotation + n * math.pi / points),
                 y + (radius if n % 2 == 0 else radius * inner) * math.sin(rotation + n * math.pi / points)) for n in range(points * 2)]
    d.line(vertices + vertices[:1], fill=color, width=width, joint="curve")


def _line(d, points, color, width=1):
    d.line(points, fill=color, width=width, joint="curve")


def _curve(d, fn, color, width=1, steps=60):
    _line(d, [fn(i / steps) for i in range(steps + 1)], color, width)


def _petal(d, x, y, length, width, angle, color):
    ca, sa = math.cos(angle), math.sin(angle)
    pts = []
    for j in range(25):
        p = TAU * j / 24
        a, b = length * (1 + math.cos(p)) / 2, width * math.sin(p) * math.sin(p / 2)
        pts.append((x + a * ca - b * sa, y + a * sa + b * ca))
    d.polygon(pts, fill=color)


def _hex(x, y, r, angle=0):
    return [(x + r * math.cos(angle + TAU * j / 6), y + r * math.sin(angle + TAU * j / 6)) for j in range(6)]


def border(k):
    """Twelve geometrically distinct stationary edge frames."""
    im = blank()
    d = ImageDraw.Draw(im)
    if k == 0:  # Art-deco folding fans.
        d.rectangle((7, 7, W - 8, H - 8), outline=(227, 190, 116, 170), width=1)
        d.rectangle((11, 11, W - 12, H - 12), outline=(255, 226, 177, 65), width=1)
        for x, y, start in [(13, 13, 0), (W-14, 13, 90), (W-14, H-14, 180), (13, H-14, 270)]:
            d.arc((x-30, y-30, x+30, y+30), start, start+90, fill=(230, 192, 120, 195), width=1)
            for a in np.linspace(start, start+90, 9):
                q = math.radians(a)
                d.line((x, y, x+30*math.cos(q), y+30*math.sin(q)), fill=(244, 205, 139, 120))
    elif k == 1:  # Postage stamp; alternating scalloped negative space.
        d.rectangle((3, 3, W-4, H-4), outline=(245, 225, 194, 125), width=9)
        for y in range(11, H-9, 14):
            d.ellipse((-2, y-4, 5, y+4), fill=(0,0,0,0))
            d.ellipse((W-6, y-4, W+1, y+4), fill=(0,0,0,0))
        for x in range(11, W-9, 14):
            d.ellipse((x-4, -2, x+4, 5), fill=(0,0,0,0))
            d.ellipse((x-4, H-6, x+4, H+1), fill=(0,0,0,0))
        d.rectangle((12, 12, W-13, H-13), outline=(232, 204, 158, 100))
        for y in range(19, H-19, 8):
            d.line((9,y,9,y+2),fill=(255,242,220,135)); d.line((W-10,y,W-10,y+2),fill=(255,242,220,135))
    elif k == 2:  # Arched window quarter ornaments.
        d.line((8,45,8,H-46), fill=(190,216,237,145)); d.line((W-9,45,W-9,H-46), fill=(190,216,237,145))
        d.line((45,8,W-46,8), fill=(190,216,237,145)); d.line((45,H-9,W-46,H-9), fill=(190,216,237,145))
        for flipx, flipy in [(False,False),(True,False),(False,True),(True,True)]:
            tile=Image.new("RGBA",(55,55)); q=ImageDraw.Draw(tile)
            q.arc((7,7,75,75),180,270,fill=(217,230,244,180),width=1)
            q.arc((14,14,69,69),180,270,fill=(161,197,222,115),width=1)
            q.line((8,43,8,8,43,8),fill=(223,233,244,170))
            q.line((13,37,23,23,37,13),fill=(197,220,242,135))
            _star(q,23,23,6,(222,234,245,170),rotation=math.pi/4)
            if flipx: tile=tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if flipy: tile=tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            im.alpha_composite(tile,(W-55 if flipx else 0,H-55 if flipy else 0))
    elif k == 3:  # Continuous Greek key rails.
        c=(134,199,192,140)
        for x,sgn in [(5,1),(W-6,-1)]:
            for y in range(14,H-22,24):
                _line(d,[(x,y),(x+9*sgn,y),(x+9*sgn,y+15),(x+3*sgn,y+15),(x+3*sgn,y+6),(x+6*sgn,y+6)],c)
        for y,sgn in [(5,1),(H-6,-1)]:
            for x in range(20,W-24,24):
                _line(d,[(x,y),(x,y+9*sgn),(x+15,y+9*sgn),(x+15,y+3*sgn),(x+6,y+3*sgn),(x+6,y+6*sgn)],c)
        d.rectangle((18,18,W-19,H-19),outline=(194,222,212,55))
    elif k == 4:  # Scalloped shell lace.
        for y in range(20,H-20,24):
            for x,sign in [(3,1),(W-4,-1)]:
                for r in (7,11): d.arc((x-r,y-r,x+r,y+r),-90 if sign==1 else 90,90 if sign==1 else 270,fill=(246,236,213,130))
                for a in (-55,-20,20,55):
                    q=math.radians(a)
                    d.line((x,y,x+sign*11*math.cos(q),y+11*math.sin(q)),fill=(249,240,222,90))
        for x in range(18,W-18,14):
            for y in (7,H-8): d.ellipse((x-1,y-1,x+1,y+1),fill=(255,240,213,190))
        d.rectangle((16,16,W-17,H-17),outline=(240,223,190,55))
    elif k == 5:  # Honeycomb, with varying filled cells.
        for side in (0,1):
            for j,y in enumerate(range(9,H,19)):
                x=(3 if j%2 else 12) if side==0 else W-1-(3 if j%2 else 12)
                p=_hex(x,y,10,math.pi/6)
                d.polygon(p,fill=(147,226,236,17 if j%4 else 38))
                d.line(p+p[:1],fill=(168,223,232,110),width=1)
        for x in range(24,W-24,12):
            d.line((x,4,x+5,4),fill=(202,238,240,110));d.line((x,H-5,x+5,H-5),fill=(202,238,240,110))
    elif k == 6:  # Cross-stitch embroidery.
        for x in (6,W-7):
            for y in range(8,H-8,8): d.line((x,y,x,y+3),fill=(248,214,187,160))
        for x in (12,W-13):
            for y in range(10,H-10,14):
                d.line((x-3,y,x+3,y+6),fill=(205,140,125,170));d.line((x+3,y,x-3,y+6),fill=(246,190,154,170))
        for y in (7,H-8):
            for x in range(22,W-22,10): d.line((x,y,x+5,y),fill=(246,208,177,145))
        for x in (12,W-13):
            for y in (13,H-14): _star(d,x,y,7,(248,223,190,200),rotation=math.pi/4)
    elif k == 7:  # Iridescent faceted glass, corner-oriented.
        colors=[(174,202,250,43),(239,181,221,52),(170,236,229,44),(251,229,186,47)]
        for side in (0,1):
            for j in range(8):
                y=j*80
                x=0 if side==0 else W-1; sign=1 if side==0 else -1
                p=[(x,y),(x+sign*(15 if j%2 else 8),y+27),(x+sign*4,y+80)]
                d.polygon(p,fill=colors[(j+side)%4]);d.line(p+[p[0]],fill=(*colors[(j+side)%4][:3],95))
        for x,y,signx,signy in [(0,0,1,1),(W-1,0,-1,1),(0,H-1,1,-1),(W-1,H-1,-1,-1)]:
            d.polygon([(x,y),(x+signx*48,y),(x+signx*24,y+signy*16),(x,y+signy*40)],fill=(219,212,249,48))
            d.line((x,y,x+signx*24,y+signy*16),fill=(244,235,255,155))
    elif k == 8:  # Flowing contour rails.
        for side in (0,1):
            for j in range(3):
                _curve(d,lambda q,s=side,n=j: ((7+n*4+2.5*math.sin(q*TAU*5+n)) if s==0 else W-8-n*4-2.5*math.sin(q*TAU*5+n),q*(H-1)),(141+25*j,173+22*j,232,105-20*j),steps=180)
        for j in range(2):
            _curve(d,lambda q,n=j:(q*(W-1),6+n*4+2*math.sin(q*TAU*3+n)),(187,207,244,110),steps=90)
            _curve(d,lambda q,n=j:(q*(W-1),H-7-n*4-2*math.sin(q*TAU*3+n)),(187,207,244,110),steps=90)
    elif k == 9:  # Editorial asymmetry, long lines and page tabs.
        d.line((10,78,10,H-30,W-54,H-30),fill=(182,215,194,120),width=1)
        d.line((38,11,W-11,11,W-11,H-88),fill=(212,226,204,165),width=1)
        d.rectangle((5,13,9,61),fill=(163,191,159,130))
        d.rectangle((W-10,H-62,W-6,H-15),fill=(216,187,132,160))
        for j in range(3): d.line((20+j*5,19,20+j*5,37-j*5),fill=(221,205,166,145))
        for j in range(3): d.line((W-39+j*5,H-20,W-39+j*5,H-38+j*5),fill=(221,205,166,145))
        d.line((17,85,17,H-54),fill=(184,215,196,40));d.line((W-18,53,W-18,H-88),fill=(184,215,196,40))
    elif k == 10:  # Pearl chain with four cabochons.
        d.rectangle((8,8,W-9,H-9),outline=(232,210,175,65))
        for y in range(15,H-14,12):
            for x in (8,W-9):
                d.ellipse((x-2,y-2,x+2,y+2),fill=(242,224,200,145));d.point((x-1,y-1),fill=(255,249,233,205))
        for x in range(18,W-17,12):
            for y in (8,H-9): d.ellipse((x-1,y-1,x+1,y+1),fill=(247,233,205,165))
        for x,y in [(W/2,8),(W/2,H-9),(8,H/2),(W-9,H/2)]:
            d.polygon([(x,y-5),(x+3,y),(x,y+5),(x-3,y)],fill=(204,185,226,140),outline=(246,227,187,200))
    elif k == 11:  # Rosette and vine filigree.
        for side in (0,1):
            sx=1 if side==0 else -1; bx=9 if side==0 else W-10
            _curve(d,lambda q:(bx+sx*3*math.sin(q*TAU*8),16+q*(H-32)),(166,195,157,100),steps=180)
            for j,y in enumerate(range(35,H-30,38)):
                a=(-.7 if j%2 else .7) + (0 if side==0 else math.pi)
                _petal(d,bx,y,8,2.5,a,(161,193,152,90))
            for y in (23,H-24):
                for a in np.linspace(0,TAU,7)[:-1]: _petal(d,bx,y,9,4,a,(239,179,176,125))
                d.ellipse((bx-3,y-3,bx+3,y+3),fill=(246,217,173,190))
        d.line((29,8,W-30,8),fill=(217,186,165,95));d.line((29,H-9,W-30,H-9),fill=(217,186,165,95))
    else:
        raise ValueError(k)
    return im


def _edge_mask(im):
    """All atmosphere overlays leave a completely clear central column."""
    arr=np.array(im)
    horizontal=np.clip((84-EDGE_X)/28,0,1)
    arr[:,:,3]=(arr[:,:,3].astype(np.float32)*horizontal).astype(np.uint8)
    return Image.fromarray(arr)


def _flake(d,x,y,r,angle,c):
    for a in range(6):
        q=angle+a*TAU/6
        ex,ey=x+r*math.cos(q),y+r*math.sin(q)
        d.line((x,y,ex,ey),fill=c)
        for sign in (-1,1):
            bx,by=x+r*.62*math.cos(q),y+r*.62*math.sin(q)
            d.line((bx,by,bx+r*.25*math.cos(q+sign*.8),by+r*.25*math.sin(q+sign*.8)),fill=c)


def atmosphere(k,t):
    """Periodic edge weather, translucent ribbons, and optical effects."""
    t=float(t)%1.0
    p=TAU*t
    im=blank();d=ImageDraw.Draw(im)
    if k == 0:  # Snow with sinusoids, wrapping hidden by fade.
        rng=np.random.default_rng(2011)
        for j in range(50):
            side=j%2;offset=rng.random();speed=1 if j%3 else 2
            phase=(offset+t*speed)%1; y=-16+(H+32)*phase
            x=rng.uniform(6,62)+7*math.sin(p+rng.uniform(0,TAU))
            if side: x=W-x
            fade=min(1,max(0,phase*12),max(0,(1-phase)*12));r=rng.uniform(.7,2.1)
            d.ellipse((x-r,y-r,x+r,y+r),fill=(226,239,255,int(135*fade)))
            if j%11==0: _flake(d,x,y,5,p*.5 if speed==2 else p,(235,246,255,int(100*fade)))
    elif k == 1:  # Fan-shaped ginkgo leaves.
        rng=np.random.default_rng(2012)
        for j in range(17):
            phase=(rng.random()+t)%1; y=-22+(H+44)*phase
            x=rng.uniform(12,53)+9*math.sin(p+rng.uniform(0,TAU))
            if j%2:x=W-x
            a=.7*math.sin(p+j); size=rng.uniform(5,10)
            fade=min(1,phase*10,(1-phase)*10);c=(233,191+int(j%3)*9,92,int(110*fade))
            points=[(x,y+size*.5)]
            for q in np.linspace(-2.8,-.35,13):
                points.append((x+size*math.cos(q+a),y+size*math.sin(q+a)))
            d.polygon(points,fill=c)
            for q in (-2.3,-1.7,-1.1,-.5): d.line((x,y+size*.5,x+size*.9*math.cos(q+a),y+size*.9*math.sin(q+a)),fill=(255,221,134,int(95*fade)))
            d.line((x,y+size*.5,x+2*math.sin(a),y+size),fill=(205,169,96,int(110*fade)))
    elif k == 2:  # Moving water caustics, continuous nonparticle design.
        dist=np.minimum(X,W-1-X)
        edge=np.exp(-(dist/39)**2)
        wave=np.sin(Y/31+2*np.sin(X/23+p)+p)*np.cos(Y/49-X/17-p)
        vein=np.exp(-(wave/.105)**2)
        alpha=edge*(vein*49+np.exp(-((wave-.58)/.2)**2)*17)
        rgb=np.stack([125+36*np.sin(V*TAU+p),204+25*np.cos(U*TAU-p),235+14*np.sin(V*TAU-p)],axis=-1)
        im=_rgba(rgb,alpha)
    elif k == 3:  # Petals, oblong shapes rotate and bend.
        rng=np.random.default_rng(2014)
        for j in range(24):
            phase=(rng.random()+t)%1;y=-20+(H+40)*phase
            x=rng.uniform(12,62)+10*math.sin(p+j*.77)
            if j%2:x=W-x
            fade=min(1,phase*10,(1-phase)*10)
            angle=p+j*.9;length=rng.uniform(7,13);width=2+2*abs(math.sin(p+j))
            _petal(d,x,y,length,width,angle,(249,177+j%3*13,196+j%2*18,int(118*fade)))
            d.line((x,y,x+length*.65*math.cos(angle),y+length*.65*math.sin(angle)),fill=(255,225,229,int(100*fade)))
    elif k == 4:  # Curving firefly trails and breathing halos.
        glow=blank();g=ImageDraw.Draw(glow)
        for j in range(16):
            side=j%2;phase=p+j*1.7
            bx=28+18*math.sin(phase);x=bx if side==0 else W-bx
            y=45+j//2*75+19*math.sin(phase*.0+p+j*.9)
            strength=.48+.52*(1+math.sin(p*2+j*.8))/2
            for n in range(9):
                q=phase-n*.07; xx=28+18*math.sin(q); yy=45+j//2*75+19*math.sin(p-n*.07+j*.9)
                if side:xx=W-xx
                d.ellipse((xx-1,yy-1,xx+1,yy+1),fill=(197,227,113,int((1-n/9)*70*strength)))
            g.ellipse((x-6,y-6,x+6,y+6),fill=(221,241,137,int(50*strength)))
            d.ellipse((x-1.5,y-1.5,x+1.5,y+1.5),fill=(246,249,182,int(185*strength)))
        im=Image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4)),im)
    elif k == 5:  # Broad satin ribbons, a pair of undulating bands.
        alpha=np.zeros((H,W),np.float32);rgb=np.zeros((H,W,3),np.float32)
        for side in (0,1):
            for j,color in [(0,(249,177,153)),(1,(230,228,244))]:
                center=15+j*17+13*np.sin(Y/113+p+side*1.2+j)
                distance=X-center if side==0 else W-1-X-center
                band=np.exp(-(distance/(5+j*2))**2)*(24+20*(1+np.cos(Y/75-p+j))/2)
                band+=np.exp(-((distance-3)/.9)**2)*18
                mask=band>alpha
                rgb[mask]=color;alpha=np.maximum(alpha,band)
        im=_rgba(rgb,alpha)
    elif k == 6:  # Side mist and crystalline snowflakes.
        mist=np.exp(-(EDGE_X/32)**2)*(9+5*np.sin(Y/90+p)+4*np.cos(Y/47-p))
        im=_rgba((193,218,242),mist);d=ImageDraw.Draw(im)
        for j in range(14):
            side=j%2;x=20+17*math.sin(p+j*.67);x=x if not side else W-x
            y=35+j//2*92+8*math.cos(p+j)
            a=int(45+37*(1+math.sin(p+j*1.8))/2)
            _flake(d,x,y,7+j%3*2,p+j,(212,233,247,a))
    elif k == 7:  # Soap rings rise; changing highlights are periodic.
        rng=np.random.default_rng(2018)
        for j in range(18):
            phase=(rng.random()-t)%1;y=-22+(H+44)*phase
            x=rng.uniform(12,60)+6*math.sin(p+j);x=x if not j%2 else W-x
            r=rng.uniform(6,15)*(1+.07*math.sin(p+j));fade=min(1,phase*9,(1-phase)*9)
            d.ellipse((x-r,y-r,x+r,y+r),outline=(189,221,241,int(67*fade)),width=1)
            d.arc((x-r,y-r,x+r,y+r),205+15*math.sin(p),282+15*math.sin(p),fill=(248,217,238,int(145*fade)),width=1)
            d.arc((x-r+2,y-r+2,x+r-2,y+r-2),35,98,fill=(183,241,222,int(100*fade)),width=1)
            d.ellipse((x-r*.4-1,y-r*.55-1,x-r*.4+1,y-r*.55+1),fill=(245,250,255,int(125*fade)))
    elif k == 8:  # Tumbling prism shards.
        rng=np.random.default_rng(2019);palette=[(176,218,246),(230,180,224),(248,216,155),(166,228,209)]
        for j in range(27):
            phase=(rng.random()+t)%1;y=-20+(H+40)*phase
            x=rng.uniform(9,61)+5*math.sin(p+j);x=x if not j%2 else W-x
            r=rng.uniform(3,7);angle=p+j;wide=.25+.75*abs(math.sin(p+j*.4))
            verts=[(x+wide*r*math.cos(angle+q*TAU/(3+j%2)),y+r*math.sin(angle+q*TAU/(3+j%2))) for q in range(3+j%2)]
            fade=min(1,phase*10,(1-phase)*10)
            d.polygon(verts,fill=(*palette[j%4],int(105*fade)))
            d.line(verts[:2],fill=(247,243,237,int(125*fade)))
    elif k == 9:  # Expanding water ripples with opacity fade at cycle boundaries.
        for j in range(10):
            phase=(t+j*.173)%1;r=4+31*phase;opacity=int(90*math.sin(math.pi*phase)**2)
            x=8 if j%2==0 else W-9;y=40+j//2*139
            for n in range(3):
                rr=r+n*7;d.ellipse((x-rr,y-rr*.48,x+rr,y+rr*.48),outline=(167+15*n,214+8*n,238,opacity//(1+n)),width=1)
    else:raise ValueError(k)
    return _edge_mask(im)


def _corner_tile(k,t):
    tile=Image.new("RGBA",(80,94));d=ImageDraw.Draw(tile)
    p=TAU*(t%1);x,y=35,36
    sway=math.sin(p)
    if k==0:  # Origami crane: two separately moving triangular wings.
        wing=9+5*math.sin(p)
        d.polygon([(x-2,y+3),(x-24,y-wing),(x-13,y+9)],fill=(191,220,239,138),outline=(223,240,250,170))
        d.polygon([(x,y+4),(x+20,y-17-wing*.5),(x+14,y+8)],fill=(218,233,246,125),outline=(231,244,251,160))
        d.polygon([(x-13,y+9),(x+7,y+4),(x+16,y+15),(x-1,y+13)],fill=(168,204,228,115))
        d.line((x+4,y+7,x+9,y-5,x+17,y-10,x+22,y-7),fill=(227,242,250,180),width=2)
        d.line((x-13,y+10,x-25,y+16),fill=(211,233,247,145))
    elif k==1:  # Daisy petals.
        angle=.15*sway
        for j in range(10): _petal(d,x,y,20,5,j*TAU/10+angle,(250,240,213,145))
        d.ellipse((x-5,y-5,x+5,y+5),fill=(229,189,105,175))
        for q in np.linspace(0,TAU,7)[:-1]:d.ellipse((x+2*math.cos(q)-.6,y+2*math.sin(q)-.6,x+2*math.cos(q)+.6,y+2*math.sin(q)+.6),fill=(255,229,151,205))
        d.line((x,y+23,x-4,y+38),fill=(156,192,145,100))
        _petal(d,x-1,y+29,9,3,3.7,(171,201,150,105))
    elif k==2:  # Butterfly wings change breadth rather than spinning.
        breadth=13+5*math.cos(p)
        for s in (-1,1):
            d.polygon([(x,y),(x+s*breadth,y-17),(x+s*(breadth+3),y-5),(x+s*8,y+6),(x+s*(breadth-1),y+18),(x+s*3,y+13)],fill=(211 if s<0 else 241,180,228,135),outline=(240,218,248,155))
            d.line((x,y,x+s*breadth,y-12),fill=(250,233,255,100))
        d.line((x,y-9,x,y+13),fill=(224,204,238,190),width=2)
        d.line((x,y-9,x-5,y-16),fill=(235,211,238,140));d.line((x,y-9,x+5,y-16),fill=(235,211,238,140))
        _star(d,13,64,3,(235,210,246,80),rotation=p)
    elif k==3:  # Two counter-rotating hexagonal rings.
        for r,a,c in [(23,p,(158,215,213,145)),(15,-p,(209,239,226,140))]:
            h=_hex(x,y,r,a);d.line(h+h[:1],fill=c,width=1)
            for xx,yy in h: d.ellipse((xx-1,yy-1,xx+1,yy+1),fill=(*c[:3],190))
        d.ellipse((x-4,y-4,x+4,y+4),outline=(216,232,201,165))
        d.line((12,69,44,69),fill=(184,219,214,65))
    elif k==4:  # Musical stave and three independently breathing notes.
        for j in range(5): d.line((12,46+j*4,64,46+j*4),fill=(206,203,232,60))
        for j in range(3):
            nx=21+j*15;ny=43+j*5+2*math.sin(p+j*.9)
            d.ellipse((nx-4,ny-2,nx+3,ny+2),fill=(211+j*11,204,239,170))
            d.line((nx+3,ny,nx+3,ny-19),fill=(227,213,241,160),width=2)
            if j<2:d.line((nx+3,ny-19,nx+10,ny-16,nx+9,ny-12),fill=(227,213,241,145),width=2)
    elif k==5:  # Laurel open wreath.
        for s in (-1,1):
            _curve(d,lambda q:(x+s*(8+13*math.sin(q*math.pi*.9)),y+25-45*q),(190,204,137,135),steps=30)
            for j in range(6):
                q=(j+.4)/6;lx=x+s*(8+13*math.sin(q*math.pi*.9));ly=y+25-45*q
                _petal(d,lx,ly,9,3.2,-math.pi/2+s*(.6+.08*sway),(181+6*j,198+4*j,137,140))
        d.line((x-6,y+27,x+6,y+27),fill=(239,221,164,150))
        _star(d,x,y+2,5,(235,219,170,130),rotation=math.pi/4+.1*sway)
    elif k==6:  # Hanging faceted diamond.
        dx=4*sway;cy=y+9
        d.line((x,6,x+dx,cy-15),fill=(197,218,241,115))
        pts=[(x+dx-11,cy-6),(x+dx-6,cy-14),(x+dx+6,cy-14),(x+dx+11,cy-6),(x+dx,cy+16)]
        d.polygon(pts,fill=(179,210,238,48),outline=(224,238,249,180))
        d.line((x+dx-11,cy-6,x+dx+11,cy-6),fill=(214,235,249,145))
        d.line((x+dx-6,cy-14,x+dx-3,cy-6,x+dx,cy+16,x+dx+3,cy-6,x+dx+6,cy-14),fill=(222,231,249,130))
        _star(d,x+dx+13,cy-8,4,(244,249,255,int(120+50*math.sin(p)**2)),rotation=p)
    elif k==7:  # Paper lantern with ribs and trailing tassel.
        dx=3*sway;cx=x+dx
        d.line((x,5,cx,19),fill=(232,191,136,140))
        d.ellipse((cx-13,20,cx+13,51),fill=(230,140,124,62),outline=(244,196,155,145))
        for q in (-.55,0,.55):d.arc((cx-10+q*7,20,cx+10+q*7,51),80,280,fill=(249,200,161,70))
        d.rectangle((cx-7,18,cx+7,21),fill=(241,212,165,150));d.rectangle((cx-7,50,cx+7,53),fill=(241,212,165,145))
        d.line((cx,53,cx+2*sway,66),fill=(238,188,147,145))
        for j in (-2,0,2):d.line((cx+2*sway+j,63,cx+3*sway+j,73),fill=(233,174,144,100))
    elif k==8:  # Compass and swaying needle.
        d.ellipse((x-22,y-22,x+22,y+22),outline=(185,208,218,120))
        d.ellipse((x-18,y-18,x+18,y+18),outline=(203,217,207,55))
        for j in range(16):
            a=j*TAU/16;r=17 if j%4==0 else 19
            d.line((x+r*math.cos(a),y+r*math.sin(a),x+21*math.cos(a),y+21*math.sin(a)),fill=(217,223,201,135))
        a=-math.pi/2+.35*sway
        needle=[(x+19*math.cos(a),y+19*math.sin(a)),(x+4*math.cos(a+math.pi/2),y+4*math.sin(a+math.pi/2)),(x-15*math.cos(a),y-15*math.sin(a)),(x-4*math.cos(a+math.pi/2),y-4*math.sin(a+math.pi/2))]
        d.polygon(needle,fill=(172,207,219,130),outline=(229,224,185,190))
    elif k==9:  # Paper airplane and dotted contrail.
        dx=3*math.cos(p);dy=2*sway
        pts=[(x-17+dx,y+10+dy),(x+21+dx,y-14+dy),(x+6+dx,y+18+dy),(x+1+dx,y+4+dy)]
        d.polygon(pts,fill=(182,228,215,110),outline=(220,243,233,170))
        d.line((x-17+dx,y+10+dy,x+21+dx,y-14+dy,x+1+dx,y+4+dy,x+6+dx,y+18+dy),fill=(212,239,229,155))
        for j in range(9):
            q=j/8;tx=28-13*q+5*math.sin(q*math.pi);ty=53+25*q
            d.ellipse((tx-.8,ty-.8,tx+.8,ty+.8),fill=(193,229,218,int(100-60*q)))
    elif k==10:  # Ribbon bow with separate tails.
        width=19+2*sway
        for s in (-1,1):
            d.polygon([(x,y),(x+s*width,y-13),(x+s*(width+2),y+7),(x,y+4)],fill=(229,168,188,110),outline=(250,212,222,160))
            d.line((x,y+2,x+s*(width-4),y-6),fill=(250,223,229,95))
            d.polygon([(x+s*2,y+5),(x+s*9,y+4),(x+s*(13+2*sway),y+31),(x+s*7,y+26),(x+s*2,y+31)],fill=(234,181,198,100),outline=(247,213,224,100))
        d.rounded_rectangle((x-4,y-3,x+4,y+7),radius=2,fill=(243,208,214,170))
    elif k==11:  # Three tulips, delicate stem sway.
        for j in range(3):
            tx=x+(j-1)*15+(2+j%2)*math.sin(p+j*.55);ty=25+abs(j-1)*7
            _curve(d,lambda q,xx=tx,yy=ty:(xx+(x-xx)*q*.65,yy+q*(73-yy)),(156,198,153,135),steps=24)
            d.polygon([(tx-7,ty-4),(tx-2,ty),(tx,ty-8),(tx+3,ty),(tx+8,ty-4),(tx+6,ty+8),(tx,ty+12),(tx-6,ty+7)],fill=(239,174+12*j,181+12*j,145),outline=(250,211,204,150))
            _petal(d,tx+(x-tx)*.4,ty+26,13,3.2,-.8 if j%2 else -2.4,(161,206,155,120))
    else:raise ValueError(k)
    return tile


def corner(k,t):
    """Four mirrored corner motifs, never entering the central column."""
    im=blank()
    for pos in range(4):
        tile=_corner_tile(k,(float(t)+pos*.125)%1)
        if pos%2:tile=tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if pos//2:tile=tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        im.alpha_composite(tile,(W-80 if pos%2 else 0,H-94 if pos//2 else 0))
    return im


def _mix(a,b,factor):
    return np.array(a,np.float32)+(np.array(b,np.float32)-np.array(a,np.float32))*np.clip(factor,0,1)[:,:,None]


def tint(k):
    """Eight distinct spatial color fields; runtime supplies 1%-10% opacity."""
    if k==0:
        radius=np.sqrt(((U-.12)*.9)**2+((V-.08)*.72)**2)
        rgb=_mix((251,211,160),(140,158,200),radius)
        rgb=rgb*(1-.12*V[:,:,None])+np.array((225,150,160))*.12*V[:,:,None]
    elif k==1:
        f=1/(1+np.exp(-((U+V)/2-.51)*6))
        rgb=_mix((116,190,202),(239,181,136),f)
    elif k==2:
        f=np.clip(np.sqrt(((U-.48)*1.3)**2+((V-.43)*1.05)**2),0,1)
        rgb=_mix((171,170,218),(244,171,155),f)
    elif k==3:
        f=np.clip(.5+.26*np.cos(U*math.pi)+.24*np.sin(V*math.pi*1.1),0,1)
        rgb=_mix((136,173,141),(232,216,160),f)
    elif k==4:
        f=np.exp(-(((U-.72)/.58)**2+((V-.38)/.42)**2))
        rgb=_mix((135,162,205),(246,206,164),f)
    elif k==5:
        a=np.exp(-(((U-.12)/.8)**2+((V-.12)/.75)**2));b=np.exp(-(((U-.86)/.7)**2+((V-.84)/.65)**2))
        rgb=np.full((H,W,3),(192,193,180),np.float32)
        rgb+=a[:,:,None]*np.array((39,-13,-38))+b[:,:,None]*np.array((-45,21,27))
    elif k==6:
        f=.3+.42*np.exp(-((U*.8+V-.48)/.3)**2)+.2*np.exp(-((U*.8+V-1.35)/.25)**2)
        rgb=_mix((244,199,179),(184,158,218),f)
    elif k==7:
        rgb=np.full((H,W,3),(225,225,216),np.float32)
        for cx,cy,sx,sy,delta in [(.14,.18,.65,.45,(-42,8,13)),(.83,.42,.55,.48,(20,-32,5)),(.38,.9,.7,.35,(18,2,-36))]:
            f=np.exp(-(((U-cx)/sx)**2+((V-cy)/sy)**2));rgb+=f[:,:,None]*np.array(delta)
    else:raise ValueError(k)
    return _rgba(rgb,255)


LAYERS={"border":border,"opacity_video":atmosphere,"corners":corner,"tint":tint}
