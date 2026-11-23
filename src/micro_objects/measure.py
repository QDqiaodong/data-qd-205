from fractions import Fraction
from .label import FOUR,holes

def measure(index,pixels,intensity,size,shape,connectivity):
    height,width=shape;sx,sy=size;occupied=set(pixels);count=len(pixels)
    xs=[p[0] for p in pixels];ys=[p[1] for p in pixels];bbox=[min(xs),min(ys),max(xs)+1,max(ys)+1]
    perimeter=Fraction(0)
    for x,y in pixels:
        for dx,dy in FOUR:
            if (x+dx,y+dy) not in occupied:perimeter+=sy if dx else sx
    total=sum(intensity[y][x] for x,y in pixels)
    return {'label':index,'pixels':[list(p) for p in pixels],'pixel_count':count,'bbox':bbox,'area':str(count*sx*sy),'edge_perimeter':str(perimeter),'centroid':[str(sum((Fraction(x)+Fraction(1,2))*sx for x in xs)/count),str(sum((Fraction(y)+Fraction(1,2))*sy for y in ys)/count)],'intensity_sum':total,'mean_intensity':str(Fraction(total,count)),'holes':holes(pixels,bbox,connectivity),'touches_border':any(x==0 or y==0 or x==width-1 or y==height-1 for x,y in pixels)}
