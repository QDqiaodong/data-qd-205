from .checks import fields,array,integer,number

def parse(data):
    fields(data,('mask','intensity','pixel_size','connectivity','selection'))
    mask=array(data['mask'],1,256)
    if not isinstance(mask[0],str) or not 1<=len(mask[0])<=256:raise ValueError('mask row required')
    width=len(mask[0])
    if any(not isinstance(row,str) or len(row)!=width or set(row)-{'0','1'} for row in mask):raise ValueError('rectangular binary mask required')
    intensity=[tuple(integer(v,0,65535) for v in array(row,width,width)) for row in array(data['intensity'],len(mask),len(mask))]
    size=tuple(number(v) for v in array(data['pixel_size'],2,2))
    if any(v<=0 for v in size):raise ValueError('positive pixel sizes required')
    connectivity=integer(data['connectivity'],4,8)
    if connectivity not in (4,8):raise ValueError('connectivity must be four or eight')
    selection=fields(data['selection'],('min_pixels','exclude_border'));minimum=integer(selection['min_pixels'],1,65536)
    if type(selection['exclude_border']) is not bool:raise ValueError('exclude_border must be boolean')
    return mask,intensity,size,connectivity,minimum,selection['exclude_border']
