from .model import parse
from .label import label
from .measure import measure

def process(data):
    mask,intensity,size,connectivity,minimum,exclude_border=parse(data);labels,components=label(mask,connectivity);objects=[];work=0
    for index,pixels in enumerate(components,1):
        xs=[x for x,y in pixels];ys=[y for x,y in pixels]
        work+=(max(xs)-min(xs)+3)*(max(ys)-min(ys)+3)
        if work>2000000:raise ValueError('topology work budget exceeded')
        obj=measure(index,pixels,intensity,size,(len(mask),len(mask[0])),connectivity);reasons=[]
        if obj['pixel_count']<minimum:reasons.append('below_min_pixels')
        if exclude_border and obj['touches_border']:reasons.append('touches_border')
        obj.update(accepted=not reasons,rejection_reasons=reasons);objects.append(obj)
    accepted={o['label'] for o in objects if o['accepted']}
    selected=[[v if v in accepted else 0 for v in row] for row in labels]
    return {'labels':labels,'objects':objects,'selected_labels':selected,'summary':{'objects':len(objects),'accepted':len(accepted),'foreground_pixels':sum(len(p) for p in components),'selected_pixels':sum(o['pixel_count'] for o in objects if o['accepted'])}}
