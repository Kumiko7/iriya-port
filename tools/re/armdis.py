import struct, capstone, sys
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
a=open(os.environ.get('ARM9', os.path.join(ROOT, 'dev', 'extracted', 'iriya1_arm9.bin')),'rb').read(); base=0x2000000
md=capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
md.skipdata = True
def dis(addr,n):
    off=addr-base
    return list(md.disasm(a[off:off+n*4],addr))
def case_targets(sw_addr, ncases):
    out={}
    for k in range(ncases):
        ins=dis(sw_addr+8+4*k,1)[0]
        out[k]=int(ins.op_str.strip('#'),16) if ins.mnemonic=='b' else None
    return out
if __name__=='__main__':
    for x in dis(int(sys.argv[1],16), int(sys.argv[2])):
        print(hex(x.address), x.mnemonic, x.op_str)
