import struct,collections,sys
def parse(d):
    hs=struct.unpack_from('<I',d,0)[0]
    p=hs; out=[]
    while p+1<len(d):
        sz,op=d[p],d[p+1]
        if op<0x40:
            out.append((p-hs,'G',(sz|op<<8,))); p+=2; continue
        if sz==0: out.append((p-hs,'ZERO',(op,))); p+=2; continue
        args=struct.unpack_from('<%dH'%(sz-1),d,p+2) if sz>1 else ()
        out.append((p-hs,op,args)); p+=2*sz
    return hs,out
if __name__=='__main__':
    c=collections.Counter(); sz=collections.defaultdict(collections.Counter); ex={}
    for f in sys.argv[1:]:
        d=open(f,'rb').read(); hs,ins=parse(d)
        for o,op,a in ins:
            c[op]+=1; sz[op][len(a)]+=1; ex.setdefault(op,[]).append((f,o,a))
    for op in sorted(c, key=lambda x: (str(type(x)),x)):
        e=ex[op][len(ex[op])//2]
        print(op if isinstance(op,str) else hex(op), c[op], dict(sz[op]), e[0][-14:], hex(e[1]), [hex(x) for x in e[2][:8]])
