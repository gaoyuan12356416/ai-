"""Procedural overlay designs approved in the 2026-09-18 reference gallery."""

import math
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 360, 640
TAU = math.tau
Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
U, V = X / W, Y / H
EDGE = np.minimum.reduce([U, 1-U, V * H/W, (1-V)*H/W])

GROUPS = {
    "border": {"title": "边框", "en": "BORDER", "prefix": "B", "accent": (167, 130, 64), "note": "静态边框 · 同一底片 · 仅展示本组效果", "names": ["香槟双线", "冷银切角", "奶油胶片", "雾紫柔边", "青玉竹影"], "descs": ["细双线与菱形收口", "银色切角与短刻度", "复古胶片与柔和纸边", "轻柔渐变包裹四周", "玉色细线与竹叶点缀"], "next": 4},
    "opacity_video": {"title": "透明视频", "en": "ATMOSPHERE", "prefix": "V", "accent": (57, 135, 130), "note": "透明循环动效 · 主体区域保持清晰", "names": ["金色浮尘", "玫瑰散景", "月蓝细雨", "青紫极光", "银白星屑"], "descs": ["边缘金色微粒缓缓浮动", "柔焦光斑缓慢升起", "细雨斜落与轻微反光", "两侧光幕轻柔流动", "细星闪烁与掠过流星"], "next": 6},
    "corners": {"title": "角标", "en": "CORNERS", "prefix": "C", "accent": (180, 104, 116), "note": "局部循环动效 · 留出人物与字幕区域", "names": ["金色星芒", "薄荷扫描", "玫瑰心跳", "银月轨道", "琥珀纸签"], "descs": ["对角星芒轻缓呼吸", "四角框线与短程扫描", "对角爱心轻轻跳动", "月牙与星点缓慢环绕", "折角丝带与纸签轻摆"], "next": 4},
    "tint": {"title": "色彩", "en": "COLOR", "prefix": "T", "accent": (123, 112, 160), "note": "静态色彩图层 · 统一按 10% 强度预览", "names": ["琥珀暖光", "冰蓝冷调", "玫瑰柔粉", "青绿电影", "紫暮双色"], "descs": ["暖金与蜜色轻染", "干净克制的冷蓝", "柔和玫瑰粉色氛围", "青绿与微暖渐变", "紫色与香槟色渐变"], "next": 8},
}

def blank():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))

def rgba(rgb, alpha):
    a = np.zeros((H, W, 4), dtype=np.uint8)
    a[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8) if isinstance(rgb, np.ndarray) else rgb
    a[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8) if isinstance(alpha, np.ndarray) else alpha
    return Image.fromarray(a)

def over(a, b):
    return Image.alpha_composite(a, b)

def polygon_star(draw, x, y, r, color, rotation=0, ratio=0.22, outline=False):
    p = []
    for k in range(8):
        angle = -math.pi/2 + k * math.pi/4 + rotation
        rr = r if k % 2 == 0 else r*ratio
        p.append((x+math.cos(angle)*rr, y+math.sin(angle)*rr))
    if outline:
        draw.line(p+[p[0]], fill=color, width=1, joint="curve")
    else:
        draw.polygon(p, fill=color)

def heart(draw, x, y, size, color, rotation=0):
    points=[]
    for a in np.linspace(0, TAU, 100):
        px=16*math.sin(a)**3 / 32 * size
        py=-(13*math.cos(a)-5*math.cos(2*a)-2*math.cos(3*a)-math.cos(4*a)) / 32 * size
        points.append((x+px*math.cos(rotation)-py*math.sin(rotation),y+px*math.sin(rotation)+py*math.cos(rotation)))
    draw.line(points+[points[0]], fill=color, width=2, joint="curve")

def border(k):
    im=blank(); d=ImageDraw.Draw(im)
    if k==0:
        gold=(239,211,151,210); faint=(239,211,151,105)
        d.rectangle((8,8,351,631), outline=faint, width=1)
        d.rectangle((13,13,346,626), outline=gold, width=2)
        d.rectangle((18,18,341,621), outline=faint, width=1)
        for cx,cy in [(13,13),(346,13),(13,626),(346,626)]:
            d.rectangle((cx-3,cy-3,cx+3,cy+3),fill=gold)
        for cy in [13,626]:
            d.line((155,cy,205,cy), fill=(20,22,23,190),width=5)
            d.polygon([(180,cy-5),(188,cy),(180,cy+5),(172,cy)], fill=gold)
            for cx in [162,198]: d.ellipse((cx-1,cy-1,cx+1,cy+1),fill=gold)
    elif k==1:
        points=[(32,10),(328,10),(350,32),(350,608),(328,630),(32,630),(10,608),(10,32)]
        d.line(points+[points[0]],fill=(10,26,40,100),width=7,joint="curve")
        d.line(points+[points[0]],fill=(217,235,247,218),width=2,joint="curve")
        for sx in [1,-1]:
            for sy in [1,-1]:
                tx=lambda x:x if sx==1 else W-x
                ty=lambda y:y if sy==1 else H-y
                d.line([(tx(16),ty(58)),(tx(16),ty(35)),(tx(36),ty(16)),(tx(58),ty(16))], fill=(154,205,222,220),width=2)
        for cy in [285,296,307,318,329,340,351]:
            d.line((10,cy,15 if cy==318 else 13,cy),fill=(220,239,255,175),width=1)
            d.line((346 if cy==318 else 349,cy,351,cy),fill=(220,239,255,175),width=1)
    elif k==2:
        d.rectangle((0,0,17,H),fill=(243,226,193,226)); d.rectangle((342,0,W,H),fill=(243,226,193,226))
        d.rectangle((0,0,W,7),fill=(245,230,203,210)); d.rectangle((0,632,W,H),fill=(245,230,203,210))
        for cy in range(18,H-12,24):
            for cx in [4,347]:
                d.rounded_rectangle((cx,cy,cx+8,cy+11),radius=2,fill=(47,37,27,182))
        d.line((19,10,19,630),fill=(241,220,183,200),width=1)
        d.line((340,10,340,630),fill=(241,220,183,200),width=1)
    elif k==3:
        rgb=np.zeros((H,W,3),np.float32)
        mix=(np.sin(V*TAU*.7+U*2)+1)/2
        rgb[:]=np.array([158,163,239])[None,None,:]*(1-mix[:,:,None])+np.array([250,175,220])[None,None,:]*mix[:,:,None]
        im=rgba(rgb,np.exp(-np.maximum(EDGE,0)*W/15)*145)
        d=ImageDraw.Draw(im)
        d.rounded_rectangle((7,7,352,632),radius=25,outline=(239,223,255,180),width=1)
        d.arc((15,15,76,76),180,270,fill=(255,244,255,215),width=2)
        d.arc((283,563,344,624),0,90,fill=(255,244,255,215),width=2)
    else:
        jade=(161,221,202,210)
        d.rectangle((10,10,349,629),outline=jade,width=1)
        d.line((15,80,15,561),fill=(138,196,174,110),width=1)
        d.line((344,80,344,561),fill=(138,196,174,110),width=1)
        for sx,sy in [(1,1),(-1,-1)]:
            tx=lambda x:x if sx==1 else W-x
            ty=lambda y:y if sy==1 else H-y
            d.line([(tx(13),ty(87)),(tx(22),ty(54)),(tx(47),ty(14))],fill=jade,width=2)
            for a,b,c,e in [(23,52,11,31),(29,42,50,29),(37,29,32,10),(19,69,36,54)]:
                pts=[(tx(a),ty(b)),(tx(c-3),ty(e+6)),(tx(c),ty(e)),(tx(c+3),ty(e+6))]
                d.polygon(pts,fill=(175,231,204,190))
        for x,y in [(10,590),(349,49)]: d.ellipse((x-3,y-3,x+3,y+3),fill=(211,226,174,230))
    return im

def atmosphere(k,t):
    t = t % 1.0
    rng=np.random.default_rng(814+k)
    im=blank(); d=ImageDraw.Draw(im)
    if k==0:
        glow=blank(); gd=ImageDraw.Draw(glow)
        for i in range(52):
            x=float(rng.uniform(6,W-6)); p=float(rng.random()); rr=float(rng.uniform(.7,2.1))
            y=p*H-22*math.sin(TAU*(t+p))
            x+=7*math.sin(TAU*(t+p))
            center=.20 if .22<x/W<.78 and .17<y/H<.84 else 1
            a=int((95+100*(.5+.5*math.sin(TAU*(t+p))))*center)
            gd.ellipse((x-rr*3,y-rr*3,x+rr*3,y+rr*3),fill=(255,203,110,a//4))
            d.ellipse((x-rr,y-rr,x+rr,y+rr),fill=(255,222,162,a))
        im=over(glow.filter(ImageFilter.GaussianBlur(3)),im)
    elif k==1:
        haze=blank(); hd=ImageDraw.Draw(haze)
        for i in range(19):
            side=-1 if i%2 else 1
            x=(25 if side<0 else W-25)+float(rng.uniform(-45,40))
            p=float(rng.random()); y=(p*(H+100)-t*(H+100))%(H+100)-50
            r=float(rng.uniform(9,26)); a=int(35+40*(.5+.5*math.sin(TAU*(t+p))))
            c=(255,187+int(rng.uniform(0,35)),211,a)
            hd.ellipse((x-r,y-r,x+r,y+r),fill=c)
            d.ellipse((x-r,y-r,x+r,y+r),outline=(255,210,224,a//2),width=1)
        im=over(haze.filter(ImageFilter.GaussianBlur(6)),im)
    elif k==2:
        for i in range(70):
            p=float(rng.random()); x=float(rng.uniform(-20,W+20)); y=(p*(H+80)+t*(H+80))%(H+80)-40
            length=float(rng.uniform(9,28)); a=50 if .18<x/W<.82 else 135
            a=int(a*(.65+.35*math.sin(TAU*(p+t))**2))
            d.line((x+8,y,x,y+length),fill=(185,222,253,a),width=1)
        a=np.exp(-EDGE*W/23)*12
        im=over(rgba((90,167,235),a),im)
    elif k==3:
        p=TAU*t
        left=10+13*np.sin(V*7+p)+9*np.sin(V*15-p)
        right=W-10+14*np.sin(V*8-p)
        wave=np.exp(-((X-left)/21)**2)*(.58+.42*np.sin(V*8+p)**2)
        wave+=np.exp(-((X-right)/25)**2)*(.55+.45*np.cos(V*6-p)**2)
        color=np.zeros((H,W,3),np.float32)
        mix=(np.sin(V*5+p)+1)/2
        color[:]=np.array([99,235,218])*(1-mix[:,:,None])+np.array([181,156,246])*mix[:,:,None]
        im=rgba(color,np.minimum(wave*118,125))
    else:
        for i in range(18):
            x=float(rng.uniform(14,70)) if i%2 else float(rng.uniform(W-70,W-14))
            y=float(rng.uniform(15,H-15)); p=float(rng.random())
            pulse=(.5+.5*math.sin(TAU*(t+p)))**2
            r=1.5+pulse*5.5
            polygon_star(d,x,y,r,(235,245,255,int(55+pulse*160)))
        for offset in [0,.5]:
            q=(t+offset)%1
            x=17+q*80; y=20+q*210
            if offset: x,y=W-x,H-y
            a=int(math.sin(math.pi*q)*170)
            sign=-1 if offset else 1
            d.line((x-sign*9,y-sign*21,x,y),fill=(190,218,254,a//2),width=2)
            polygon_star(d,x,y,3,(241,249,255,a))
    return im

def corner(k,t):
    t = t % 1.0
    im=blank(); d=ImageDraw.Draw(im)
    p=TAU*t
    if k==0:
        for x,y,phase in [(35,40,0),(W-35,H-40,math.pi)]:
            pulse=.5+.5*math.sin(p+phase)
            polygon_star(d,x,y,16+pulse*5,(255,226,164,int(150+pulse*80)))
            polygon_star(d,x+19,y+17,5+pulse*2,(255,238,193,180))
            d.arc((x-25,y-25,x+25,y+25),30,130,fill=(235,204,143,160),width=1)
    elif k==1:
        for flipx,flipy in [(False,False),(True,False),(False,True),(True,True)]:
            xx=lambda x:W-x if flipx else x
            yy=lambda y:H-y if flipy else y
            d.line([(xx(17),yy(52)),(xx(17),yy(17)),(xx(52),yy(17))],fill=(158,245,219,235),width=2)
            a=21+22*(.5+.5*math.sin(p))
            d.line((xx(a),yy(23),xx(a+7),yy(23)),fill=(220,255,241,200),width=2)
            d.ellipse((xx(25)-2,yy(32)-2,xx(25)+2,yy(32)+2),fill=(144,222,212,150))
    elif k==2:
        for x,y,phase in [(W-39,43,0),(39,H-40,math.pi)]:
            beat=(.5+.5*math.cos(p+phase))**3
            heart(d,x,y,30+beat*5,(255,186,207,235),-.15)
            heart(d,x-18,y+17,13,(251,214,224,175),.22)
            polygon_star(d,x+19,y+18,3+beat*2,(255,231,237,200))
    elif k==3:
        for x,y,phase in [(38,42,0),(W-38,H-42,math.pi)]:
            d.arc((x-19,y-19,x+19,y+19),45,310,fill=(211,231,248,200),width=2)
            d.arc((x-13,y-18,x+24,y+17),71,285,fill=(220,238,250,215),width=1)
            a=p+phase
            sx=x+25*math.cos(a); sy=y+25*math.sin(a)
            polygon_star(d,sx,sy,5,(223,239,255,225))
            d.arc((x-27,y-27,x+27,y+27),12,95,fill=(173,199,229,110),width=1)
    else:
        for flipx,flipy in [(True,False),(False,True)]:
            sway=2.5*math.sin(p)
            pts=[(20,10),(46,10),(46+sway,55),(33+sway,45),(20+sway,55)]
            pts=[(W-x if flipx else x,H-y if flipy else y) for x,y in pts]
            d.polygon(pts,fill=(232,184,112,225))
            d.line(pts[:3],fill=(255,227,175,245),width=1)
            xx=W-33 if flipx else 33; yy=26 if not flipy else H-26
            polygon_star(d,xx,yy,5,(255,241,205,240))
            sx=W-54 if flipx else 54; sy=20 if not flipy else H-20
            d.ellipse((sx-2,sy-2,sx+2,sy+2),fill=(255,224,169,220))
    return im

def tint(k):
    if k==0: top,bottom=[243,166,64],[246,196,104]
    elif k==1: top,bottom=[83,146,224],[135,185,242]
    elif k==2: top,bottom=[244,135,180],[244,180,209]
    elif k==3: top,bottom=[29,152,136],[214,180,107]
    else: top,bottom=[147,101,217],[233,188,132]
    mix=V[:,:,None]
    return rgba(np.array(top)*(1-mix)+np.array(bottom)*mix,255)
