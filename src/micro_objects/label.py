from collections import deque

FOUR=((1,0),(-1,0),(0,1),(0,-1))
def neighbors(connectivity):return FOUR if connectivity==4 else FOUR+((1,1),(1,-1),(-1,1),(-1,-1))

def label(mask,connectivity):
    height=len(mask);width=len(mask[0]);labels=[[0]*width for _ in mask];components=[];steps=neighbors(connectivity)
    for y in range(height):
        for x in range(width):
            if mask[y][x]!='1' or labels[y][x]:continue
            index=len(components)+1;labels[y][x]=index;pending=deque([(x,y)]);pixels=[]
            while pending:
                p=pending.popleft();pixels.append(p)
                for dx,dy in steps:
                    a,b=p[0]+dx,p[1]+dy
                    if 0<=a<width and 0<=b<height and not labels[b][a] and mask[b][a]=='1':
                        labels[b][a]=index;pending.append((a,b))
            components.append(sorted(pixels,key=lambda p:(p[1],p[0])))
    return labels,components

def holes(pixels,bbox,foreground_connectivity):
    # Other objects are background for THIS object's topology. Padding connects
    # the outside; complementary connectivity avoids contradictory diagonal holes.
    x0,y0,x1,y1=bbox;occupied=set(pixels);seen=set();regions=0;steps=neighbors(8 if foreground_connectivity==4 else 4)
    for y in range(y0-1,y1+1):
        for x in range(x0-1,x1+1):
            if (x,y) in occupied or (x,y) in seen:continue
            pending=[(x,y)];seen.add((x,y));regions+=1
            while pending:
                a,b=pending.pop()
                for dx,dy in steps:
                    p=(a+dx,b+dy)
                    if x0-1<=p[0]<=x1 and y0-1<=p[1]<=y1 and p not in occupied and p not in seen:
                        seen.add(p);pending.append(p)
    return regions-1
