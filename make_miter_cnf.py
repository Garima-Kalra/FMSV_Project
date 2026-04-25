import argparse, json, re
from collections import defaultdict

# ---------------- Liberty parsing ----------------

def parse_liberty_functions(lib_path):
    text = open(lib_path).read()
    cell_funcs = {}
    i = 0
    n = len(text)
    while True:
        m = re.search(r'cell\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*\{', text[i:])
        if not m:
            break
        cname = m.group(1)
        start = i + m.end() - 1
        depth = 0
        j = start
        while j < n:
            if text[j] == '{': depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = text[start:j+1]
        # parse pin direction/function
        pins = {}
        for pm in re.finditer(r'pin\s*\(\s*([A-Za-z0-9_]+)\s*\)\s*\{', block):
            pname = pm.group(1)
            ps = pm.end()-1
            d=0; k=ps
            while k < len(block):
                if block[k]=='{': d+=1
                elif block[k]=='}':
                    d-=1
                    if d==0: break
                k+=1
            pblock = block[ps:k+1]
            md = re.search(r'direction\s*:\s*([a-zA-Z_]+)\s*;', pblock)
            mf = re.search(r'function\s*:\s*"([^"]+)"\s*;', pblock)
            pins[pname] = {
                'direction': md.group(1) if md else None,
                'function': mf.group(1) if mf else None,
            }
        cell_funcs[cname] = pins
        i = j+1
    return cell_funcs

# ---------------- Boolean expr parsing ----------------
TOKEN_RE = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*|\!|\'|\(|\)|\+|\*|\&|\|)')

class Parser:
    def __init__(self, s):
        self.tokens = [t for t in TOKEN_RE.findall(s)]
        self.i = 0
    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None
    def eat(self, tok=None):
        t = self.peek()
        if tok is not None and t != tok:
            raise ValueError(f'Expected {tok}, got {t}')
        self.i += 1
        return t
    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise ValueError(f'Unexpected token {self.peek()}')
        return node
    def parse_or(self):
        node = self.parse_and()
        while self.peek() in ('+', '|'):
            self.eat()
            node = ('or', node, self.parse_and())
        return node
    def parse_and(self): 
        # cnf.const(faulty_override, fault['sa'])
        node = self.parse_unary()
        while True:
            t = self.peek()
            if t in ('*', '&'):
                self.eat()
                node = ('and', node, self.parse_unary())
            elif t and (re.match(r'[A-Za-z_]', t) or t in ('!', '(')):
                # implicit AND
                node = ('and', node, self.parse_unary())
            else:
                break
        return node
    def parse_unary(self):
        if self.peek() == '!':
            self.eat('!')
            return ('not', self.parse_unary())
        node = self.parse_primary()
        while self.peek() == "'":
            self.eat("'")
            node = ('not', node)
        return node
    def parse_primary(self):
        t = self.peek()
        if t == '(':
            self.eat('(')
            node = self.parse_or()
            self.eat(')')
            return node
        if t is None:
            raise ValueError('Unexpected EOF')
        self.eat()
        return ('var', t)

def parse_expr(s):
    return Parser(s).parse()

# ---------------- CNF builder ----------------
class CNF:
    def __init__(self):
        self.var_count = 0
        self.clauses = []
        self.names = {}
    def new_var(self, name=None):
        self.var_count += 1
        if name:
            self.names[name] = self.var_count
        return self.var_count
    def add(self, *lits):
        self.clauses.append(list(lits))
    def equate(self, a, b):
        self.add(-a, b)
        self.add(a, -b)
    def const(self, a, val):
        self.add(a if val else -a)
    def tseitin_expr(self, ast, env, memo=None):
        if memo is None: memo = {}
        key = str(ast)
        if key in memo: return memo[key]
        kind = ast[0]
        if kind == 'var':
            v = env[ast[1]]
        elif kind == 'not':
            x = self.tseitin_expr(ast[1], env, memo)
            v = self.new_var()
            # v <-> ~x
            self.add(-v, -x)
            self.add(v, x)
        elif kind == 'and':
            a = self.tseitin_expr(ast[1], env, memo)
            b = self.tseitin_expr(ast[2], env, memo)
            v = self.new_var()
            self.add(-v, a)
            self.add(-v, b)
            self.add(v, -a, -b)
        elif kind == 'or':
            a = self.tseitin_expr(ast[1], env, memo)
            b = self.tseitin_expr(ast[2], env, memo)
            v = self.new_var()
            self.add(-a, v)
            self.add(-b, v)
            self.add(a, b, -v)
        else:
            raise ValueError(ast)
        memo[key] = v
        return v

# ---------------- Netlist handling ----------------
def top_module(design):
    mods = design['modules']
    tops = [n for n,m in mods.items() if m.get('attributes', {}).get('top') == 1 or m.get('attributes', {}).get('top') == '1']
    return tops[0] if tops else next(iter(mods))

def get_io(module):
    inputs, outputs = [], []
    for pname, pdata in module['ports'].items():
        bits = [b for b in pdata['bits'] if isinstance(b, int)]
        if pdata['direction'] == 'input':
            inputs.extend((pname, idx, b) for idx,b in enumerate(bits))
        elif pdata['direction'] == 'output':
            outputs.extend((pname, idx, b) for idx,b in enumerate(bits))
    return inputs, outputs

def build_instance_cnf(cnf, module, libcells, prefix, shared_pi_vars=None, fault=None):
    # 1. Map all nets to CNF variables
    bit_var = {}
    for netname, info in module.get('netnames', {}).items():
        for b in info['bits']:
            if isinstance(b, int) and b not in bit_var:
                bit_var[b] = cnf.new_var(f'{prefix}:bit:{b}:{netname}')

    inputs, outputs = get_io(module)
    pi_map = {}
    
    # 2. Handle shared Primary Inputs
    if shared_pi_vars is not None:
        for pname, idx, bit in inputs:
            key = f'{pname}[{idx}]'
            bit_var[bit] = shared_pi_vars[key]
            pi_map[key] = shared_pi_vars[key]
    else:
        for pname, idx, bit in inputs:
            key = f'{pname}[{idx}]'
            pi_map[key] = bit_var[bit]

    # 3. Create the fault override (the "Stuck-At" site)
    faulty_override = None
    if fault is not None:
        faulty_override = cnf.new_var(f'{prefix}:fault_site:{fault["net"]}')
        cnf.const(faulty_override, fault['sa'])

    # 4. Process all gates/cells
    for cell_name, cell in module['cells'].items():
        ctype = cell['type']
        pins = libcells[ctype]
        env = {}
        out_pin = None
        for pin, bits in cell['connections'].items():
            b = bits[0]
            if b == '0' or b == 0:
                v = cnf.new_var(); cnf.const(v, 0)
            elif b == '1' or b == 1:
                v = cnf.new_var(); cnf.const(v, 1)
            else:
                # If this is an input to a gate and it's the faulted net, use the override
                if fault is not None and pins.get(pin, {}).get('direction') == 'input' and b == fault['net']:
                    v = faulty_override
                else:
                    v = bit_var[b]
            env[pin] = v
            if pins.get(pin, {}).get('direction') == 'output':
                out_pin = pin
        
        # Build logic for the gate
        ast = parse_expr(pins[out_pin]['function'])
        expr_v = cnf.tseitin_expr(ast, env)
        cnf.equate(env[out_pin], expr_v)

    # 5. FINAL PIECE: Map Primary Outputs for observation
    po_map = {}
    for pname, idx, bit in outputs:
        key = f'{pname}[{idx}]'
        # If the fault is exactly on this output wire, the miter must observe the fault_site
        if fault is not None and bit == fault['net']:
            po_map[key] = faulty_override
        else:
            # Otherwise, observe the normal logic net
            po_map[key] = bit_var[bit]

    return {'bit_var': bit_var, 'inputs': pi_map, 'outputs': po_map}


def main():
    ap = argparse.ArgumentParser(description='Build miter CNF directly from Yosys JSON + Liberty')
    ap.add_argument('gold_json')
    ap.add_argument('faulty_json')
    ap.add_argument('liberty')
    ap.add_argument('--fault-net', type=int, help='faulted net id in faulty design')
    ap.add_argument('--sa', type=int, choices=[0,1], help='stuck-at value')
    ap.add_argument('-o', '--out', default='miter.cnf')
    ap.add_argument('--map-out', default='miter_map.json')
    args = ap.parse_args()

    gold = json.load(open(args.gold_json))
    faulty = json.load(open(args.faulty_json))
    libcells = parse_liberty_functions(args.liberty)
    gm = gold['modules'][top_module(gold)]
    fm = faulty['modules'][top_module(faulty)]

    cnf = CNF()
    # shared PI variables by port name/index
    shared_pi = {}
    for pname, idx, bit in get_io(gm)[0]:
        shared_pi[f'{pname}[{idx}]'] = cnf.new_var(f'PI:{pname}[{idx}]')

# 1. Capture Gold logic
    start_gold = len(cnf.clauses)
    g = build_instance_cnf(cnf, gm, libcells, 'gold', shared_pi_vars=shared_pi, fault=None)
    end_gold = len(cnf.clauses)

    # 2. Capture Faulty logic
    start_faulty = len(cnf.clauses)
    fault_spec = None
    if args.fault_net is not None:
        fault_spec = {'net': args.fault_net, 'sa': args.sa}
    f = build_instance_cnf(cnf, fm, libcells, 'faulty', shared_pi_vars=shared_pi, fault=fault_spec)
    end_faulty = len(cnf.clauses)

    # # 3. Print the segments to terminal
    # print(f"\n--- GOLD CNF CLAUSES ---")
    # for c in cnf.clauses[start_gold:end_gold]:
    #     print(c)

    # print(f"\n--- FAULTY CNF CLAUSES (Net {args.fault_net} SA{args.sa}) ---")
    # for c in cnf.clauses[start_faulty:end_faulty]:
    #     print(c)

    # output comparator: at least one primary output differs
    diffs = []
    for key in sorted(g['outputs'].keys()):
        gv = g['outputs'][key]
        fv = f['outputs'][key]
        d = cnf.new_var(f'diff:{key}')
        # d <-> xor(gv, fv)
        cnf.add(-gv, -fv, -d)
        cnf.add(gv, fv, -d)
        cnf.add(-gv, fv, d)
        cnf.add(gv, -fv, d)
        diffs.append(d)
    cnf.add(*diffs)  # force at least one differing output

    with open(args.out, 'w') as fp:
        fp.write(f'p cnf {cnf.var_count} {len(cnf.clauses)}\n')
        for c in cnf.clauses:
            fp.write(' '.join(map(str, c)) + ' 0\n')

    mapping = {
        'pi_vars': shared_pi,
        'gold_output_vars': g['outputs'],
        'faulty_output_vars': f['outputs'],
        'named_vars': cnf.names,
    }
    with open(args.map_out, 'w') as fp:
        json.dump(mapping, fp, indent=2)

    print(f'Wrote {args.out} with {cnf.var_count} vars and {len(cnf.clauses)} clauses')

if __name__ == '__main__':
    main()
