import re
from fractions import Fraction

def fields(obj,required,optional=()):
    if not isinstance(obj,dict) or set(required)-obj.keys() or obj.keys()-set(required)-set(optional):
        raise ValueError(f'expected fields {required}; optional {optional}')
    return obj

def array(value,low,high):
    if not isinstance(value,list) or not low<=len(value)<=high:raise ValueError(f'array size {low}..{high} required')
    return value

def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:raise ValueError(f'integer {low}..{high} required')
    return value

def ident(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,64}',value):raise ValueError('invalid id')
    return value

def unique(values):
    if len(set(values))!=len(values):raise ValueError('duplicate identity')

def number(value):
    if not isinstance(value,str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,8})(?:[.][0-9]{1,6})?',value):raise ValueError('bounded decimal string required')
    return Fraction(value)

def vector(value,size):return tuple(number(x) for x in array(value,size,size))


def fraction(value,positive=False):
    n,d=array(value,2,2);integer(n,-100000,100000);integer(d,1,100000)
    result=Fraction(n,d)
    if positive and result<=0:raise ValueError('positive fraction required')
    return result
