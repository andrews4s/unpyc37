"""
Decompiler for Python3.7.
Decompile a module or a function using the decompile() function

>>> from unpyc3 import decompile
>>> def foo(x, y, z=3, *args):
...    global g
...    for i, j in zip(x, y):
...        if z == i + j or args[i] == j:
...            g = i, j
...            return
...    
>>> print(decompile(foo))

def foo(x, y, z=3, *args):
    global g
    for i, j in zip(x, y):
        if z == i + j or args[i] == j:
            g = i, j
            return
>>>
"""
from __future__ import annotations

from typing import Union, Iterable, Any, List

__all__ = ['decompile']


def set_trace(trace_function):
    global current_trace
    current_trace = trace_function if trace_function else _trace


def get_trace():
    global current_trace
    return None if current_trace == _trace else current_trace


def trace(*args):
    global current_trace
    if current_trace:
        current_trace(*args)


def _trace(*args):
    pass


current_trace = _trace

# TODO:
# - Support for keyword-only arguments
# - Handle assert statements better
# - (Partly done) Nice spacing between function/class declarations

import dis
from array import array
from opcode import opname, opmap, HAVE_ARGUMENT, cmp_op
import inspect

import struct
import sys

# Masks for code object's co_flag attribute
VARARGS = 4
VARKEYWORDS = 8

# Put opcode names in the global namespace
for name, val in opmap.items():
    globals()[name] = val
PRINT_EXPR = 70

# These opcodes will generate a statement. This is used in the first
# pass (in Code.find_else) to find which POP_JUMP_IF_* instructions
# are jumps to the else clause of an if statement
stmt_opcodes = {
    SETUP_LOOP, BREAK_LOOP, CONTINUE_LOOP,
    SETUP_FINALLY, END_FINALLY,
    SETUP_EXCEPT, POP_EXCEPT,
    SETUP_WITH,
    POP_BLOCK,
    STORE_FAST, DELETE_FAST,
    STORE_DEREF, DELETE_DEREF,
    STORE_GLOBAL, DELETE_GLOBAL,
    STORE_NAME, DELETE_NAME,
    STORE_ATTR, DELETE_ATTR,
    IMPORT_NAME, IMPORT_FROM,
    RETURN_VALUE, YIELD_VALUE,
    RAISE_VARARGS,
    STORE_SUBSCR, DELETE_SUBSCR,
}

# Conditional branching opcode that make up if statements and and/or
# expressions
pop_jump_if_opcodes = (POP_JUMP_IF_TRUE, POP_JUMP_IF_FALSE)

# These opcodes indicate that a pop_jump_if_x to the address just
# after them is an else-jump
else_jump_opcodes = (
    JUMP_FORWARD, RETURN_VALUE, JUMP_ABSOLUTE,
    SETUP_LOOP, RAISE_VARARGS, POP_TOP
)

# These opcodes indicate for loop rather than while loop
for_jump_opcodes = (
    GET_ITER, FOR_ITER, GET_ANEXT
)

unpack_stmt_opcodes = {STORE_NAME, STORE_FAST, STORE_SUBSCR, STORE_GLOBAL, STORE_DEREF, STORE_ATTR}
unpack_terminators = stmt_opcodes - unpack_stmt_opcodes

def read_code(stream):
    # This helper is needed in order for the PEP 302 emulation to 
    # correctly handle compiled files
    # Note: stream must be opened in "rb" mode
    import marshal

    if sys.version_info < (3, 4):
        import imp
        runtime_magic = imp.get_magic()
    else:
        import importlib.util
        runtime_magic = importlib.util.MAGIC_NUMBER

    magic = stream.read(4)
    if magic != runtime_magic:
        print("*** Warning: file has wrong magic number ***")

    flags = 0
    if sys.version_info >= (3, 7):
        flags = struct.unpack('i', stream.read(4))[0]

    if flags & 1:
        stream.read(4)
        stream.read(4)
    else:
        stream.read(4)  # Skip timestamp
        if sys.version_info >= (3, 3):
            stream.read(4)  # Skip rawsize
            return marshal.load(stream)


def dec_module(path) -> Suite:
    if path.endswith(".py"):
        if sys.version_info < (3, 6):
            import imp
            path = imp.cache_from_source(path)
        else:
            import importlib.util
            path = importlib.util.cache_from_source(path)
    elif not path.endswith(".pyc") and not path.endswith(".pyo"):
        raise ValueError("path must point to a .py or .pyc file")
    with open(path, "rb") as stream:
        code_obj = read_code(stream)
        code = Code(code_obj)
        return code.get_suite(include_declarations=False, look_for_docstring=True)


def decompile(obj) -> Union[Suite, PyStatement]:
    """
    Decompile obj if it is a module object, a function or a
    code object. If obj is a string, it is assumed to be the path
    to a python module.
    """
    if isinstance(obj, str):
        return dec_module(obj)
    if inspect.iscode(obj):
        code = Code(obj)
        return code.get_suite()
    if inspect.isfunction(obj):
        code = Code(obj.__code__)
        defaults = obj.__defaults__
        kwdefaults = obj.__kwdefaults__
        return DefStatement(code, defaults, kwdefaults, obj.__closure__)
    elif inspect.ismodule(obj):
        return dec_module(obj.__file__)
    else:
        msg = "Object must be string, module, function or code object"
        raise TypeError(msg)


class Indent:
    def __init__(self, indent_level=0, indent_step=4):
        self.level = indent_level
        self.step = indent_step

    def write(self, pattern, *args, **kwargs):
        if args or kwargs:
            pattern = pattern.format(*args, **kwargs)
        return self.indent(pattern)

    def __add__(self, indent_increase):
        return type(self)(self.level + indent_increase, self.step)


class IndentPrint(Indent):
    def indent(self, string):
        print(" " * self.step * self.level + string)


class IndentString(Indent):
    def __init__(self, indent_level=0, indent_step=4, lines=None):
        Indent.__init__(self, indent_level, indent_step)
        if lines is None:
            self.lines = []
        else:
            self.lines = lines

    def __add__(self, indent_increase):
        return type(self)(self.level + indent_increase, self.step, self.lines)

    def sep(self):
        if not self.lines or self.lines[-1]:
            self.lines.append("")

    def indent(self, string):
        self.lines.append(" " * self.step * self.level + string)

    def __str__(self):
        return "\n".join(self.lines)


class Stack:
    def __init__(self):
        self._stack = []
        self._counts = {}

    def __bool__(self):
        return bool(self._stack)

    def __len__(self):
        return len(self._stack)

    def __contains__(self, val):
        return self.get_count(val) > 0

    def get_count(self, obj):
        return self._counts.get(id(obj), 0)

    def set_count(self, obj, val):
        if val:
            self._counts[id(obj)] = val
        else:
            del self._counts[id(obj)]

    def pop1(self):
        val = None
        if self._stack:
            val = self._stack.pop()
        else:
            raise Exception('Empty stack popped!')
        self.set_count(val, self.get_count(val) - 1)
        return val

    def pop(self, count=None):
        if count is None:
            val = self.pop1()
            return val
        else:
            vals = [self.pop1() for i in range(count)]
            vals.reverse()
            return vals

    def push(self, *args):
        for val in args:
            self.set_count(val, self.get_count(val) + 1)
            self._stack.append(val)

    def peek(self, count=None):
        if count is None:
            return self._stack[-1]
        else:
            return self._stack[-count:]


def code_walker(code):
    l = len(code)
    code = array('B', code)
    oparg = 0
    i = 0
    extended_arg = 0

    while i < l:
        op = code[i]
        offset = 1
        if sys.version_info >= (3, 6):
            oparg = code[i + offset]
            offset += 1
        elif op >= HAVE_ARGUMENT:
            oparg = code[i + offset] + code[i + offset + 1] * 256 + extended_arg
            extended_arg = 0
            offset += 2
        if op == EXTENDED_ARG:
            if sys.version_info >= (3, 6):
                op = code[i + offset]
                offset += 1
                oparg <<= 8
                oparg |= code[i + offset]
                offset += 1
            else:
                extended_arg = oparg * 65536
        yield i, (op, oparg)
        i += offset

def SPyNot(o):
    if isinstance(o, PyNot):
        return o.operand
    elif isinstance(o, PyBooleanAnd):
        if o.allowCollision:
            return PyBooleanOr(SPyNot(o.left), SPyNot(o.right), True)
    elif isinstance(o, PyBooleanOr):
        if o.allowCollision:
            return PyBooleanAnd(SPyNot(o.left), SPyNot(o.right), True)
    return PyNot(o)

class CodeFlags(object):
    def __init__(self, cf):
        self.flags = cf

    @property
    def optimized(self):
        return self.flags & 0x1

    @property
    def new_local(self):
        return self.flags & 0x2

    @property
    def varargs(self):
        return self.flags & 0x4

    @property
    def varkwargs(self):
        return self.flags & 0x8
    @property
    def nested(self):
        return self.flags & 0x10

    @property
    def generator(self):
        return self.flags & 0x20

    @property
    def no_free(self):
        return self.flags & 0x40

    @property
    def coroutine(self):
        return self.flags & 0x80

    @property
    def iterable_coroutine(self):
        return self.flags & 0x100

    @property
    def async_generator(self):
        return self.flags & 0x200


class Code:
    def __init__(self, code_obj, parent=None):
        self.code_obj = code_obj
        self.parent = parent
        self.derefnames = [PyName(v)
                           for v in code_obj.co_cellvars + code_obj.co_freevars]
        self.consts = list(map(PyConst, code_obj.co_consts))
        self.names = list(map(PyName, code_obj.co_names))
        self.varnames = list(map(PyName, code_obj.co_varnames))
        self.instr_seq = list(code_walker(code_obj.co_code))
        self.instr_map = {addr: i for i, (addr, _) in enumerate(self.instr_seq)}
        self.name = code_obj.co_name
        self.globals = []
        self.nonlocals = []
        self.loops = []
        self.annotationd = False
        self.linemap = []
        self.lineno = []
        self.start_chained_jumps = []
        self.inner_chained_jumps = []
        self.end_chained_jumps = []
        self.statement_jumps = []
        self.ternaryop_jumps = []
        self.qcjumps = []
        self.find_else()
        
        curinstr = 0
        l = len(code_obj.co_lnotab)
        curlineno = code_obj.co_firstlineno
        if l > 0 and code_obj.co_lnotab[0] == 0:
            curlineno = curlineno + code_obj.co_lnotab[1]
            i = 2
        else:
            i = 0
        self.linemap.append(curinstr)
        self.lineno.append(curlineno)
        while i < l:
            curinstr = curinstr + code_obj.co_lnotab[i]
            curlineno = curlineno + code_obj.co_lnotab[i +1]
            if code_obj.co_lnotab[i] != 127 and code_obj.co_lnotab[i +1] != 127:
                self.linemap.append(curinstr)
                self.lineno.append(curlineno)
            i = i + 2
        self.linemap = tuple(self.linemap)
        self.lineno = tuple(self.lineno)
        self.find_jumps()
        self.start_chained_jumps = tuple(self.start_chained_jumps)
        self.inner_chained_jumps = tuple(self.inner_chained_jumps)
        self.end_chained_jumps = tuple(self.end_chained_jumps)
        #self.statement_jumps = tuple(self.statement_jumps)
        self.ternaryop_jumps = tuple(self.ternaryop_jumps)
        self.flags: CodeFlags = CodeFlags(code_obj.co_flags)
        self.annotations = None#parent code sends these names when MAKE_FUNCTION has flag 8

    def __getitem__(self, instr_index):
        if 0 <= instr_index < len(self.instr_seq):
            return Address(self, instr_index)

    def __iter__(self):
        for i in range(len(self.instr_seq)):
            yield Address(self, i)

    def show(self):
        for addr in self:
            print(addr)

    def address(self, addr):
        return self[self.instr_map[addr]]

    def iscellvar(self, i):
        return i < len(self.code_obj.co_cellvars)

    def find_jumps(self):
        def proc_chained(self, addr: Address) -> Address:
            if addr[-3] and \
                    addr[-1].opcode == COMPARE_OP and \
                    addr[-2].opcode == ROT_THREE and \
                    addr[-3].opcode == DUP_TOP:
                self.start_chained_jumps.append(addr)
                jump_addr = addr.jump()
                curaddr = addr[1]
                while True:
                    if curaddr.opcode in pop_jump_if_opcodes:
                        if curaddr[-3].opcode == DUP_TOP and curaddr[-2].opcode == ROT_THREE:
                            if curaddr.arg == addr.arg:
                                self.inner_chained_jumps.append(curaddr)
                                addr = curaddr
                        elif curaddr[2] == addr.jump():
                            self.end_chained_jumps.append(curaddr)
                            return curaddr
                        else:
                            curaddr = proc_chained(self, curaddr)
                            if curaddr.jump()[-1].opcode == JUMP_FORWARD:# (a if b else c)
                                self.ternaryop_jumps.append(curaddr)
                    curaddr = curaddr[1]
            return addr
        def pj_start_true(addr: Address)-> Address:
            next_addr = addr[1]
            if next_addr.opcode == JUMP_FORWARD:
                if next_addr.addr in self.linemap:# if a: pass
                    return next_addr
                elif next_addr.arg == 0:# if (a if b else True):
                    addr = next_addr
                    next_addr = next_addr[1]
            if addr[4] and addr[2].opcode == POP_TOP and\
                    next_addr.opcode in (JUMP_ABSOLUTE, JUMP_FORWARD):
                    #addr in self.end_chained_jumps:
                if addr.opcode == POP_JUMP_IF_FALSE:# (a<b<c)
                    assert addr[3].opcode in (JUMP_ABSOLUTE, JUMP_FORWARD)
                    return addr[4]
                else:# not(a<b<c)
                    return addr[3]
            return next_addr
        def count_returns(starta, enda)->int:
            c = 0
            while starta <= enda:
                if starta.opcode != JUMP_ABSOLUTE:
                    j = starta.jump()
                    if j:
                        if j < starta:
                            c -= 1
                        else:
                            starta = j
                    elif starta.opcode == RETURN_VALUE:
                        c = c + 1
                starta = starta[1]
            return c
        i = 0
        while i < len(self.instr_seq):
            addr = Address(self, i)
            opcode, arg = addr
            jt = addr.jump()
            if jt:
                if opcode == SETUP_LOOP:
                    end_addr = jt[-1]
                    isforloop = False
                    
                    #   detect:
                    #if ...:
                    #   ...
                    #   while ...:
                    #       ...
                    #else:
                    curaddr = Address(self, 0)
                    while curaddr < addr:
                        if curaddr.opcode in pop_jump_if_opcodes:
                            if addr.addr < curaddr.arg < end_addr.addr:
                                curaddr = curaddr.jump()
                                jt = curaddr[-1]
                                end_addr = jt[-1]
                                break
                        curaddr = curaddr[1]
                    
                    curaddr = addr[1]
                    x = 0
                    while True:
                        if curaddr >= end_addr or curaddr.opcode in stmt_opcodes:
                            break
                        if x and curaddr.opcode == GET_ITER:#generator
                            x -= 1
                        elif curaddr.opcode in for_jump_opcodes:
                            isforloop = True
                            break
                        elif curaddr.opcode == JUMP_ABSOLUTE:
                            cur_addr = curaddr.jump()
                            if cur_addr.opcode in for_jump_opcodes:
                                isforloop = True
                            break
                        elif curaddr.opcode == MAKE_FUNCTION:#generator
                            x += 1
                        curaddr = curaddr[1]
                    end_cond = 0
                    if isforloop:
                        end_cond = -1
                    else:
                        curaddr = addr[1]
                        while curaddr < end_addr:
                            if curaddr.opcode in stmt_opcodes:
                                break
                            if curaddr.opcode in pop_jump_if_opcodes:
                                if curaddr.jump().opcode == POP_BLOCK or curaddr.jump() == end_addr:
                                    end_cond = curaddr.index
                            curaddr = curaddr[1]
                        if end_cond > 0:
                            i = end_cond
                            dto = Address(self, end_cond)
                            self.statement_jumps.append(dto)
                            curaddr = addr[1]
                            while curaddr < dto:
                                if curaddr.opcode in pop_jump_if_opcodes:
                                    curaddr = proc_chained(self, curaddr)
                                    curjump = curaddr.jump()
                                    if curjump[-1].opcode == JUMP_FORWARD:# (a if b else c)
                                        x = curaddr[1]
                                        while x < curjump:
                                            if x.opcode in pop_jump_if_opcodes and\
                                                    x.arg == curaddr.arg:# aa or (a if b else True)
                                                x = None
                                                break
                                            x = x[1]
                                        if x:
                                            self.ternaryop_jumps.append(curaddr)
                                curaddr = curaddr[1]
                    if end_addr.opcode == POP_BLOCK:
                        jt = end_addr
                    self.loops.append((addr.index, end_cond, jt.index))
                elif opcode in pop_jump_if_opcodes:
                    next_stmt = addr.seek_stmt(None)
                    #find start true
                    curaddr = addr
                    lastjump = None
                    x = None
                    while curaddr < next_stmt:
                        if curaddr.opcode in pop_jump_if_opcodes:
                            if curaddr.arg > next_stmt.addr or curaddr.arg < addr.addr or\
                                    (curaddr[2] == curaddr.jump() and curaddr[1].opcode == JUMP_FORWARD and curaddr[1].jump() > next_stmt):#  <--   if a: pass else:
                                if lastjump is None:
                                    lastjump = curaddr
                                    if curaddr.arg < addr.addr and next_stmt.is_continue_jump:
                                        x = curaddr# jump to true suite
                                elif lastjump.arg == curaddr.arg:
                                    lastjump = curaddr
                                elif x is not None:
                                    if x.arg == lastjump.arg:
                                        c = curaddr.jump()
                                        if (len(self.statement_jumps) and curaddr.arg >= self.statement_jumps[-1].addr) or\
                                                (len(self.loops) and c.index >= self.loops[-1][2]):
                                            #lastjump = x
                                            x = None
                                            break
                                        if c.opcode == JUMP_ABSOLUTE and c.is_continue_jump and\
                                                c.addr in self.linemap and\
                                                c[-1].opcode == JUMP_FORWARD and c[-2].opcode != POP_TOP:
                                            #lastjump = x
                                            x = None
                                            break
                                        lastjump = curaddr
                                    else:
                                        #lastjump = x
                                        x = None
                                        break
                                else:
                                    break
                        curaddr = curaddr[1]
                    
                    if self.name in ('<listcomp>','<setcomp>','<dictcomp>','<genexpr>'):
                        self.statement_jumps.append(lastjump)
                        lastjump = None
                    elif self.name == '<lambda>':
                        lastjump = None
                    if lastjump:
                        qcjumps = []
                        start_true = pj_start_true(lastjump)
                        curjump = addr
                        dto = addr
                        while curjump < start_true:
                            curjump = proc_chained(self, curjump)
                            x = curjump.jump()[-1]
                            if curjump.addr < curjump.arg <= next_stmt.addr and\
                                    x.opcode == JUMP_FORWARD and\
                                    x[-1].opcode != POP_TOP:
                                curaddr = curjump[1]
                                while curaddr < x:
                                    if curaddr.opcode in pop_jump_if_opcodes and\
                                            curaddr.arg == curjump.arg:# aa or (a if b else True)
                                        curaddr = None
                                        break
                                    curaddr = curaddr[1]
                                if curaddr:
                                    if x.jump() > next_stmt or x.addr in self.linemap:#  if a: pass else:
                                        self.statement_jumps.append(curjump)
                                    else:
                                        self.ternaryop_jumps.append(curjump)
                                        if x > dto:
                                            dto = x.jump()[-2]#-2 in case if (a if b else True):
                            elif curjump.arg == start_true.addr:# if a or
                                dto = lastjump
                            elif curjump.addr < curjump.arg < start_true.addr:
                                if x > dto:
                                    dto = x
                            else:
                                if dto <= curjump:
                                    if pj_start_true(curjump).addr in self.linemap:
                                        if curjump not in self.statement_jumps:
                                            curaddr = curjump[1]
                                            if curjump.is_continue_jump:
                                                #detect if a: if b or c:
                                                if curjump != lastjump and curjump.arg != lastjump.arg:# only or
                                                    if start_true.opcode == JUMP_ABSOLUTE and start_true.arg == curjump.arg:
                                                        while curaddr < lastjump:
                                                            if curaddr.opcode in pop_jump_if_opcodes and\
                                                                    curaddr.arg > lastjump.arg:
                                                                curaddr = None
                                                                break
                                                            curaddr = curaddr[1]
                                                        if curaddr is None:
                                                            curaddr = curjump#flag statement jump
                                                        else:
                                                            dto = lastjump
                                                            curaddr = None#flag no statement
                                                            qcjumps.append(curjump)
                                            elif x.opcode == JUMP_FORWARD or\
                                                    (x.opcode == JUMP_ABSOLUTE and x.arg > curjump.arg):
                                                while curaddr <= lastjump:# < x:
                                                    if curaddr.opcode in pop_jump_if_opcodes and\
                                                            curaddr.arg == curjump.arg:# if a and b:...else:
                                                        curaddr = None
                                                        break
                                                    curaddr = curaddr[1]
                                            if curaddr:
                                                self.statement_jumps.append(curjump)# just if a:
                                    elif curjump == lastjump:
                                        if next_stmt.opcode == RETURN_VALUE:
                                            self.ternaryop_jumps.append(curjump)# return (a if b else c)
                                    if curjump in self.statement_jumps and len(qcjumps):
                                        if curjump == lastjump:
                                            if len(self.statement_jumps) < 2 or not self.statement_jumps[-2].is_continue_jump:
                                                enda = Address(self, self.loops[-1][2])
                                                if len(self.statement_jumps) > 1 and self.statement_jumps[-2] > anda:
                                                    enda = self.statement_jumps[-2].jump()
                                                c = 0
                                                if x.opcode != JUMP_ABSOLUTE or x.arg < enda.addr:
                                                    if x.opcode == JUMP_ABSOLUTE and x != next_stmt:
                                                        if x.arg < x.addr:
                                                            starta = x[1]
                                                            c = 2
                                                        else:
                                                            starta = x.jump()
                                                            c = 1
                                                    elif x.opcode == JUMP_FORWARD:
                                                        starta = x.jump()
                                                        c = 1
                                                    elif x.opcode == RETURN_VALUE:
                                                        starta = x[1]
                                                        c = 2
                                                    else:
                                                        starta = x[1]
                                                        c = 1
                                                c = count_returns(starta, enda) - c
                                                while c > 0 and len(qcjumps):
                                                    x = qcjumps[-1]
                                                    self.statement_jumps.append(x)
                                                    qcjumps.remove(x)
                                                    c = c - 1
                                        self.qcjumps = self.qcjumps + qcjumps
                                        qcjumps = []
                            curjump = curjump[1]
                            while curjump < start_true:
                                if curjump.opcode in pop_jump_if_opcodes:
                                    break
                                curjump = curjump[1]
                        i = start_true.index - 1
                    else:
                        curjump = addr
                        while curjump < next_stmt:
                            curjump = proc_chained(self, curjump)
                            if curjump.addr < curjump.arg <= next_stmt.addr:
                                x = curjump.jump()[-1]
                                if x.opcode == JUMP_FORWARD:
                                    curaddr = curjump[1]
                                    while curaddr < x:
                                        if curaddr.opcode in pop_jump_if_opcodes and\
                                                curaddr.arg == curjump.arg:# aa or (a if b else True)
                                            curaddr = None
                                            break
                                        curaddr = curaddr[1]
                                    if curaddr:
                                        if x.addr in self.linemap:
                                            self.statement_jumps.append(curjump)# if a: pass else:
                                        else:
                                            self.ternaryop_jumps.append(curjump)
                            elif curjump.arg > next_stmt.addr and self.name == '<lambda>':
                                self.ternaryop_jumps.append(curjump)# return (a if b else c)
                            curjump = curjump[1]
                            while curjump < next_stmt:
                                if curjump.opcode in pop_jump_if_opcodes:
                                    break
                                curjump = curjump[1]
                        i = next_stmt.index - 1
            i = i + 1

    def find_else(self):
        jumps = {}
        for addr in self:
            opcode, arg = addr
            if opcode in pop_jump_if_opcodes:
                jump_addr = self.address(arg)
                if (jump_addr[-1].opcode in else_jump_opcodes
                        or jump_addr.opcode == FOR_ITER):
                    jumps[jump_addr] = addr
            elif opcode == JUMP_ABSOLUTE:
                jump_addr = self.address(arg)
                if jump_addr in jumps:
                    jumps[addr] = jumps[jump_addr]
            elif opcode == JUMP_FORWARD:
                jump_addr = addr[1] + arg
                if jump_addr in jumps:
                    jumps[addr] = jumps[jump_addr]
        self.else_jumps = set(jumps.values())

    def get_suite(self, include_declarations=True, look_for_docstring=False) -> Suite:
        dec = SuiteDecompiler(self[0])
        trace('\nname = '+self.name)
        ts = ''
        for g in self.instr_seq:
            aop,aarg = g[1]
            if g[0] in self.linemap:
                ts = ts + '\n'+str(self.lineno[self.linemap.index(g[0])])+': '
            ts = ts + (' '+str(g[0])+': '+opname[aop]+'('+str(aarg)+'), ')
        trace(ts)
        trace('\nstatement_jumps:')
        for ttt in self.statement_jumps:
            trace(ttt)
        trace('\nternaryop_jumps:')
        for ttt in self.ternaryop_jumps:
            trace(ttt)
        dec.run()
        first_stmt = dec.suite and dec.suite[0]
        # Change __doc__ = "docstring" to "docstring"
        if look_for_docstring and isinstance(first_stmt, AssignStatement):
            chain = first_stmt.chain
            if len(chain) == 2 and str(chain[0]) == "__doc__":
                dec.suite[0] = DocString(first_stmt.chain[1].val)
        if include_declarations and (self.globals or self.nonlocals or self.annotations):
            suite = Suite()
            if self.globals:
                stmt = "global " + ", ".join(map(str, self.globals))
                suite.add_statement(SimpleStatement(stmt))
            if self.nonlocals:
                stmt = "nonlocal " + ", ".join(map(str, self.nonlocals))
                suite.add_statement(SimpleStatement(stmt))
            if self.annotations is not None:
                #The annotations in nested functions are not evaluated and not stored but affect code and flags in parent function
                #def fa():
                #    from ss import aa
                #    def fb():
                #        t:aa = 0
                unused_deref_names = [x.name for x in self.derefnames]
                for addr in self:
                    opcode, arg = addr
                    if opcode in (LOAD_DEREF,STORE_DEREF, DELETE_DEREF, LOAD_CLOSURE):
                        name = self.derefnames[arg].name
                        if name in unused_deref_names:
                            unused_deref_names.remove(name)
                childvarnames = [x.name for x in self.varnames]
                if len(childvarnames) > 0:
                    for t in self.annotations:
                        if isinstance(t, PyName):
                            t = t.name
                            if not t.startswith("__") and (t in unused_deref_names) and t != "self":
                                stmt = childvarnames[0] + ":" + t + "#Inserted by decompiler to adjust code flags. Remove this line."
                                suite.add_statement(SimpleStatement(stmt))
            for stmt in dec.suite:
                suite.add_statement(stmt)
            return suite
        else:
            return dec.suite

    def declare_global(self, name):
        """
        Declare name as a global.  Called by STORE_GLOBAL and
        DELETE_GLOBAL
        """
        if name not in self.globals:
            self.globals.append(name)

    def ensure_global(self, name):
        """
        Declare name as global only if it is also a local variable
        name in one of the surrounding code objects.  This is called
        by LOAD_GLOBAL
        """
        if self.name not in code_map.keys():
            parent = self.parent
            while parent:
                if name in parent.varnames or name in parent.globals:
                    return self.declare_global(name)
                parent = parent.parent

    def declare_nonlocal(self, name):
        """
        Declare name as nonlocal.  Called by STORE_DEREF and
        DELETE_DEREF (but only when the name denotes a free variable,
        not a cell one).
        """
        if name not in self.nonlocals:
            self.nonlocals.append(name)



class Address:
    def __init__(self, code, instr_index):
        self.code = code
        self.index = instr_index
        self.addr, (self.opcode, self.arg) = code.instr_seq[instr_index]

    def __le__(self, other):
        return isinstance(other, type(self)) and self.index <= other.index

    def __ge__(self, other):
        return isinstance(other, type(self)) and self.index >= other.index

    def __eq__(self, other):
        return (isinstance(other, type(self))
                and self.code == other.code and self.index == other.index)

    def __lt__(self, other):
        return other is None or (isinstance(other, type(self))
                                 and self.code == other.code and self.index < other.index)

    def __str__(self):
        mark = "* " if self in self.code.else_jumps else "  "
        jump = self.jump()
        jt = '  '
        arg = self.arg or "  "
        jdest = '\t(to {})'.format(jump.addr) if jump and jump.addr != self.arg else ''
        val = ''
        op = opname[self.opcode].ljust(18, ' ')
        try:

            val = len(self.code.globals) and self.code.globals[self.arg] and self.arg + 1 < len(self.code.globals) if 'GLOBAL' in op else \
                self.code.names[self.arg] if 'ATTR' in op else \
                    self.code.names[self.arg] if 'NAME' in op else \
                        self.code.names[self.arg] if 'LOAD_METHOD' in op else \
                            self.code.consts[self.arg] if 'CONST' in op else \
                                self.code.varnames[self.arg] if 'FAST' in op else \
                                    self.code.derefnames[self.arg] if 'DEREF' in op else \
                                        cmp_op[self.arg] if 'COMPARE' in op else ''
            if val != '':
                val = '\t({})'.format(val)
        except:
            pass

        return "{}{}\t{}\t{}\t{}{}{}".format(
            jt,
            mark,
            self.addr,
            op,
            arg,
            jdest,
            val
        )

    def __add__(self, delta):
        return self.code.address(self.addr + delta)

    def __getitem__(self, index) -> Address:
        return self.code[self.index + index]

    def __iter__(self):
        yield self.opcode
        yield self.arg

    def __hash__(self):
        return hash((self.code, self.index))

    @property
    def is_else_jump(self):
        return self in self.code.else_jumps

    @property
    def is_continue_jump(self):
        if self.opcode in (POP_JUMP_IF_TRUE, POP_JUMP_IF_FALSE, JUMP_ABSOLUTE):
            jump_addr = self.jump()
            if jump_addr.opcode == FOR_ITER or jump_addr[-1].opcode == SETUP_LOOP:
                return True
        return False
    
    @property
    def is_statement(self):
        if self.opcode in stmt_opcodes or\
                self.opcode in (JUMP_ABSOLUTE, JUMP_FORWARD) and self.addr in self.code.linemap:
            return True
        if self.opcode == POP_TOP:
            if self[-1] and self[-1].opcode in (JUMP_ABSOLUTE, JUMP_FORWARD, ROT_TWO):
                return False
            return True
        return False
    
    def change_instr(self, opcode, arg=None):
        self.code.instr_seq[self.index] = (self.addr, (opcode, arg))

    def jump(self) -> Address:
        opcode = self.opcode
        if opcode in dis.hasjrel:
            return self[1] + self.arg
        elif opcode in dis.hasjabs:
            return self.code.address(self.arg)

    def seek(self, opcode: Iterable, increment: int, end: Address = None) -> Address:
        if self == end:
            return None
        if not isinstance(opcode, Iterable):
            opcode = (opcode,)
        a = self[increment]
        while a and a != end:
            if a.opcode in opcode:
                return a
            a = a[increment]

    def seek_back(self, opcode: Union[Iterable, int], end: Address = None) -> Address:
        return self.seek(opcode, -1, end)

    def seek_forward(self, opcode: Union[Iterable, int], end: Address = None) -> Address:
        return self.seek(opcode, 1, end)
    
    def seek_stmt(self, end: Address) ->Address:
        a = self
        while a and (end is None or a < end):
            if a.is_statement:
                return a
            a = a[1]
        return None

    def last_loop_context(self):
        lcontext = (-1,-1,-1)
        for lcontexti in self.code.loops:
            if lcontexti[0] <= self.index <= lcontexti[2]:
                if lcontext[0] < lcontexti[0]:
                    lcontext = lcontexti
        return lcontext


class AsyncMixin:
    def __init__(self):
        self.is_async = False

    @property
    def async_prefix(self):
        return 'async ' if self.is_async else ''


class AwaitableMixin:

    def __init__(self):
        self.is_awaited = False

    @property
    def await_prefix(self):
        return 'await ' if self.is_awaited else ''


class PyExpr:
    def wrap(self, condition=True):
        if condition:
            return "({})".format(self)
        else:
            return str(self)

    def store(self, dec, dest):
        chain = dec.assignment_chain
        chain.append(dest)
        if self not in dec.stack:
            chain.append(self)
            dec.suite.add_statement(AssignStatement(chain))
            dec.assignment_chain = []

    def on_pop(self, dec : SuiteDecompiler):
        dec.write(str(self))


class PyConst(PyExpr):

    def __init__(self, val):
        self.val = val
        if isinstance(val, int):
            self.precedence=14
        else:
            self.precedence = 100

    def __str__(self):
        if self.val == 1e10000:
            return '1e10000'
        elif isinstance(self.val, frozenset):
            l = list(self.val)
            l.sort()
            vals = ', '.join(map(repr,l))
            return f'{{{vals}}}'
        elif isinstance(self.val, str) and len(self.val) > 20 and '\0' not in self.val and '\x01' not in self.val:
            splt = self.val.split('\n')
            if len(splt) > 1:
                return '\"\"\"' + '\n'.join(map(lambda s: s.replace('\\', '\\\\').replace('"', '\\"'), splt)) \
                       + '\"\"\"'
        return repr(self.val)

    def __iter__(self):
        return iter(self.val)

    def __eq__(self, other):
        return isinstance(other, PyConst) and self.val == other.val


class PyFormatValue(PyConst):
    def __init__(self, val):
        super().__init__(val)
        self.formatter = ''

    @staticmethod
    def fmt(string):
        return f'f\'{string}\''

    def base(self):
        return f'{{{self.val}{self.formatter}}}'

    def __str__(self):
        return self.fmt(self.base())

class PyFormatString(PyExpr):
    precedence = 100

    def __init__(self, params):
        super().__init__()
        self.params = params

    def __str__(self):
        return "f'{}'".format(''.join([
            p.base().replace('\'', '\"') if isinstance(p, PyFormatValue) else
            p.name if isinstance(p, PyName) else
            repr(p.val+'\"')[1:-2].replace('{','{{').replace('}','}}')
            for p in self.params])
        )


class PyTuple(PyExpr):
    precedence = 0

    def __init__(self, values):
        self.values = values

    def __str__(self):
        if not self.values:
            return "()"
        valstr = [val.wrap(val.precedence <= self.precedence)
                  for val in self.values]
        if len(valstr) == 1:
            return '(' + valstr[0] + "," + ')'
        else:
            return '(' + ", ".join(valstr) + ')'

    def __iter__(self):
        return iter(self.values)

    def wrap(self, condition=True):
        return str(self)


class PyList(PyExpr):
    precedence = 16

    def __init__(self, values):
        self.values = values

    def __str__(self):
        valstr = ", ".join(val.wrap(val.precedence <= 0)
                           for val in self.values)
        return "[{}]".format(valstr)

    def __iter__(self):
        return iter(self.values)


class PySet(PyExpr):
    precedence = 16

    def __init__(self, values):
        self.values = values

    def __str__(self):
        valstr = ", ".join(val.wrap(val.precedence <= 0)
                           for val in self.values)
        return "{{{}}}".format(valstr)

    def __iter__(self):
        return iter(self.values)


class PyDict(PyExpr):
    precedence = 16

    def __init__(self):
        self.items = []

    def set_item(self, key, val):
        self.items.append((key, val))

    def __str__(self):
        itemstr = ", ".join(f"{kv[0]}: {kv[1]}" if len(kv) == 2 else str(kv[0]) for kv in self.items)
        return f"{{{itemstr}}}"


class PyName(PyExpr,AwaitableMixin):
    precedence = 100

    def __init__(self, name):
        AwaitableMixin.__init__(self)
        self.name = name

    def __str__(self):
        return f'{self.await_prefix}{self.name}'

    def __eq__(self, other):
        return isinstance(other, type(self)) and self.name == other.name


class PyUnaryOp(PyExpr):
    def __init__(self, operand):
        self.operand = operand

    def __str__(self):
        opstr = self.operand.wrap(self.operand.precedence < self.precedence)
        return self.pattern.format(opstr)

    @classmethod
    def instr(cls, stack):
        stack.push(cls(stack.pop()))


class PyBinaryOp(PyExpr):
    def __init__(self, left, right):
        self.left = left
        self.right = right

    def wrap_left(self):
        return self.left.wrap(self.left.precedence < self.precedence)

    def wrap_right(self):
        return self.right.wrap(self.right.precedence <= self.precedence)

    def __str__(self):
        return self.pattern.format(self.wrap_left(), self.wrap_right())

    @classmethod
    def instr(cls, stack):
        right = stack.pop()
        left = stack.pop()
        stack.push(cls(left, right))


class PySubscript(PyBinaryOp):
    precedence = 15
    pattern = "{}[{}]"

    def wrap_right(self):
        return str(self.right)


class PySlice(PyExpr):
    precedence = 1

    def __init__(self, args):
        assert len(args) in (2, 3)
        if len(args) == 2:
            self.start, self.stop = args
            self.step = None
        else:
            self.start, self.stop, self.step = args
        if self.start == PyConst(None):
            self.start = ""
        if self.stop == PyConst(None):
            self.stop = ""

    def __str__(self):
        if self.step is None:
            return "{}:{}".format(self.start, self.stop)
        else:
            return "{}:{}:{}".format(self.start, self.stop, self.step)


class PyCompare(PyExpr):
    precedence = 6

    def __init__(self, complist):
        self.complist = complist

    def __str__(self):
        return " ".join(x if i % 2 else x.wrap(x.precedence <= 6)
                        for i, x in enumerate(self.complist))

    def extends(self, other):
        if not isinstance(other, PyCompare):
            return False
        else:
            return self.complist[0] == other.complist[-1]

    def chain(self, other):
        return PyCompare(self.complist + other.complist[1:])


class PyBooleanAnd(PyBinaryOp):
    precedence = 4
    pattern = "{} and {}"

    def __init__(self, left, right, allowCollision = None):
        super().__init__(left, right)
        if allowCollision is None:
            self.allowCollision = False
            if isinstance(left, PyNot):
                if not(isinstance(right, PyCompare) and right.complist[1].startswith('is')):
                    self.allowCollision = True
            elif isinstance(right, PyNot):
                if not(isinstance(left, PyCompare) and left.complist[1].startswith('is')):
                    self.allowCollision = True
        else:
            self.allowCollision = allowCollision

class PyBooleanOr(PyBinaryOp):
    precedence = 3
    pattern = "{} or {}"

    def __init__(self, left, right, allowCollision = None):
        super().__init__(left, right)
        if allowCollision is None:
            self.allowCollision = False
            if isinstance(left, PyNot):
                if not(isinstance(right, PyCompare) and right.complist[1].startswith('is')):
                    self.allowCollision = True
            elif isinstance(right, PyNot):
                if not(isinstance(left, PyCompare) and left.complist[1].startswith('is')):
                    self.allowCollision = True
        else:
            self.allowCollision = allowCollision

class PyIfElse(PyExpr):
    precedence = 2

    def __init__(self, cond, true_expr, false_expr):
        self.cond = cond
        self.true_expr = true_expr
        self.false_expr = false_expr

    def __str__(self):
        p = self.precedence
        cond_str = self.cond.wrap(self.cond.precedence <= p)
        true_str = self.true_expr.wrap(self.true_expr.precedence <= p)
        false_str = self.false_expr.wrap(self.false_expr.precedence < p)
        return "{} if {} else {}".format(true_str, cond_str, false_str)


class PyAttribute(PyExpr):
    precedence = 15

    def __init__(self, expr, attrname):
        self.expr = expr
        self.attrname = attrname

    def __str__(self):
        expr_str = self.expr.wrap(self.expr.precedence < self.precedence)
        attrname = self.attrname

        if isinstance(self.expr, PyName) and self.expr.name == 'self':
            __ = attrname.name.find('__')
            if __ > 0:
                attrname = PyName(self.attrname.name[__:])
        return "{}.{}".format(expr_str, attrname)


class PyCallFunction(PyExpr, AwaitableMixin):
    precedence = 15

    def __init__(self, func: PyAttribute, args: list, kwargs: list, varargs=None, varkw=None):
        AwaitableMixin.__init__(self)
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.varargs = varargs if not varargs or isinstance(varargs,Iterable) else {varargs}
        self.varkw = varkw if not varkw or isinstance(varkw,Iterable) else {varkw}

    def __str__(self):
        funcstr = self.func.wrap(self.func.precedence < self.precedence)
        if hasattr(self.args, '__iter__') and len(self.args) == 1 and not (self.kwargs or self.varargs
                                                                           or self.varkw):
            arg = self.args[0]
            if isinstance(arg, PyGenExpr):
                # Only one pair of brackets arount a single arg genexpr
                return "{}{}".format(funcstr, arg)
        args = [x.wrap(x.precedence <= 0) for x in self.args]
        if self.varargs is not None:
            for varargs in self.varargs:
                args.append("*{}".format(varargs))
        args.extend("{}={}".format(str(k).replace('\'', ''), v.wrap(v.precedence <= 0))
                    for k, v in self.kwargs)
        if self.varkw is not None:
            for varkw in self.varkw:
                args.append("**{}".format(varkw))
        return "{}{}({})".format(self.await_prefix, funcstr, ", ".join(args))


class FunctionDefinition:
    def __init__(self, code: Code, defaults, kwdefaults, closure, paramobjs=None, annotations=None):
        self.code = code
        self.defaults = defaults
        self.kwdefaults = kwdefaults
        self.closure = closure
        self.paramobjs = paramobjs if paramobjs else {}
        self.annotations = annotations if annotations else None

    def is_coroutine(self):
        return self.code.code_obj.co_flags & 0x100

    def getparams(self):
        code_obj = self.code.code_obj
        l = code_obj.co_argcount
        params = []
        for name in code_obj.co_varnames[:l]:
            if name in self.paramobjs:
                params.append('{}:{}'.format(name, str(self.paramobjs[name])))
            else:
                params.append(name)
        if self.defaults:
            for i, arg in enumerate(reversed(self.defaults)):
                name = params[-i - 1]
                if name in self.paramobjs:
                    params[-i - 1] = "{}:{}={}".format(name, str(self.paramobjs[name]), arg)
                else:
                    params[-i - 1] = "{}={}".format(name, arg)
        kwcount = code_obj.co_kwonlyargcount
        kwparams = []
        if kwcount:
            for i in range(kwcount):
                name = code_obj.co_varnames[l + i]
                if name in self.kwdefaults and name in self.paramobjs:
                    kwparams.append("{}:{}={}".format(name, self.paramobjs[name], self.kwdefaults[name]))
                elif name in self.kwdefaults:
                    kwparams.append("{}={}".format(name, self.kwdefaults[name]))
                else:
                    kwparams.append(name)
            l += kwcount
        if code_obj.co_flags & VARARGS:
            name = code_obj.co_varnames[l]
            if name in self.paramobjs:
                params.append(f'*{name}:{str(self.paramobjs[name])}')
            else:
                params.append(f'*{name}')
            l += 1
        elif kwparams:
            params.append("*")
        params.extend(kwparams)
        if code_obj.co_flags & VARKEYWORDS:
            name = code_obj.co_varnames[l]
            if name in self.paramobjs:
                params.append(f'**{name}:{str(self.paramobjs[name])}')
            else:
                params.append(f'**{name}')

        return params

    def getreturn(self):
        if self.paramobjs and 'return' in self.paramobjs:
            return self.paramobjs['return']
        return None


class PyLambda(PyExpr, FunctionDefinition):
    precedence = 1

    def __str__(self):
        suite = self.code.get_suite()
        params = ", ".join(self.getparams())
        if len(suite.statements) > 0:
            def strip_return(val):
                return val[len("return "):] if val.startswith('return') else val

            def strip_yield_none(val):
                return '(yield)' if val == 'yield None' else val

            if isinstance(suite[0], IfStatement):
                end = suite[1] if len(suite) > 1 else PyConst(None)
                expr = "{} if {} else {}".format(
                    strip_return(str(suite[0].true_suite)),
                    str(suite[0].cond),
                    strip_return(str(end))
                )
            else:
                expr = strip_return(str(suite[0]))
                expr = strip_yield_none(expr)
        else:
            expr = "None"
        return "lambda {}: {}".format(params, expr)


class PyComp(PyExpr):
    """
    Abstraction for list, set, dict comprehensions and generator expressions
    """
    precedence = 16

    def __init__(self, code, defaults, kwdefaults, closure, paramobjs={}, annotations=[]):
        assert not defaults and not kwdefaults
        self.code = code
        code[0].change_instr(NOP)
        last_i = len(code.instr_seq) - 1
        code[last_i].change_instr(NOP)
        self.annotations = annotations

    def set_iterable(self, iterable):
        self.code.varnames[0] = iterable

    def __str__(self):
        suite = self.code.get_suite()
        return self.pattern.format(suite.gen_display())


class PyListComp(PyComp):
    pattern = "[{}]"


class PySetComp(PyComp):
    pattern = "{{{}}}"


class PyKeyValue(PyBinaryOp):
    """This is only to create dict comprehensions"""
    precedence = 1
    pattern = "{}: {}"


class PyDictComp(PyComp):
    pattern = "{{{}}}"


class PyGenExpr(PyComp):
    precedence = 16
    pattern = "({})"

    def __init__(self, code, defaults, kwdefaults, closure, paramobjs={}, annotations=[]):
        self.code = code


class PyYield(PyExpr):
    precedence = 0

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return "(yield {})".format(self.value)


class PyYieldFrom(PyExpr):
    precedence = 0

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return "(yield from {})".format(self.value)


class PyStarred(PyExpr):
    """Used in unpacking assigments"""
    precedence = 15

    def __init__(self, expr):
        self.expr = expr

    def __str__(self):
        es = self.expr.wrap(self.expr.precedence < self.precedence)
        return "*{}".format(es)


code_map = {
    '<lambda>': PyLambda,
    '<listcomp>': PyListComp,
    '<setcomp>': PySetComp,
    '<dictcomp>': PyDictComp,
    '<genexpr>': PyGenExpr,
}

unary_ops = [
    ('UNARY_POSITIVE', 'Positive', '+{}', 13),
    ('UNARY_NEGATIVE', 'Negative', '-{}', 13),
    ('UNARY_NOT', 'Not', 'not {}', 5),
    ('UNARY_INVERT', 'Invert', '~{}', 13),
]

binary_ops = [
    ('POWER', 'Power', '{}**{}', 14, '{} **= {}'),
    ('MULTIPLY', 'Multiply', '{}*{}', 12, '{} *= {}'),
    ('FLOOR_DIVIDE', 'FloorDivide', '{}//{}', 12, '{} //= {}'),
    ('TRUE_DIVIDE', 'TrueDivide', '{}/{}', 12, '{} /= {}'),
    ('MODULO', 'Modulo', '{} % {}', 12, '{} %= {}'),
    ('ADD', 'Add', '{} + {}', 11, '{} += {}'),
    ('SUBTRACT', 'Subtract', '{} - {}', 11, '{} -= {}'),
    ('SUBSCR', 'Subscript', '{}[{}]', 15, None),
    ('LSHIFT', 'LeftShift', '{} << {}', 10, '{} <<= {}'),
    ('RSHIFT', 'RightShift', '{} >> {}', 10, '{} >>= {}'),
    ('AND', 'And', '{} & {}', 9, '{} &= {}'),
    ('XOR', 'Xor', '{} ^ {}', 8, '{} ^= {}'),
    ('OR', 'Or', '{} | {}', 7, '{} |= {}'),
    ('MATRIX_MULTIPLY', 'MatrixMultiply', '{} @ {}', 12, '{} @= {}'),
]


class PyStatement(object):
    def __str__(self):
        istr = IndentString()
        self.display(istr)
        return str(istr)

    def wrap(self, condition=True):
        if condition:
            assert not condition
            return "({})".format(self)
        else:
            return str(self)

    def on_pop(self, dec):
        # dec.write("#ERROR: Unexpected context 'on_pop': pop on statement:  ")
        pass


class DocString(PyStatement):
    def __init__(self, string):
        self.string = string

    def display(self, indent):
        if '\n' not in self.string:
            indent.write(repr(self.string))
        else:
            if "'''" not in self.string:
                fence = "'''"
            else:
                fence = '"""'
            lines = self.string.split('\n')
            text = '\n'.join(l.encode('unicode_escape').decode().replace(fence,'\\'+fence)
                             for l in lines)
            docstring = "{0}{1}{0}".format(fence, text)
            indent.write(docstring)


class AssignStatement(PyStatement):
    def __init__(self, chain):
        self.chain = chain

    def display(self, indent):
        indent.write(" = ".join(map(str, self.chain)))


class InPlaceOp(PyStatement):
    def __init__(self, left, right):
        self.right = right
        self.left = left

    def store(self, dec, dest):
        # assert dest is self.left
        dec.suite.add_statement(self)

    def display(self, indent):
        indent.write(self.pattern, self.left, self.right)

    @classmethod
    def instr(cls, stack):
        right = stack.pop()
        left = stack.pop()
        stack.push(cls(left, right))


class Unpack:
    precedence = 50

    def __init__(self, val, length, star_index=None):
        self.val = val
        self.length = length
        self.star_index = star_index
        self.dests = []

    def store(self, dec, dest):
        if len(self.dests) == self.star_index:
            dest = PyStarred(dest)
        self.dests.append(dest)
        if len(self.dests) == self.length:
            dec.stack.push(self.val)
            dec.store(PyTuple(self.dests))


class ImportStatement(PyStatement):
    alias = ""
    precedence = 100

    def __init__(self, name, level, fromlist):
        self.name = name
        self.alias = name
        self.level = level
        self.fromlist = fromlist
        self.aslist = []

    def store(self, dec: SuiteDecompiler, dest):
        self.alias = dest
        dec.suite.add_statement(self)

    def on_pop(self, dec):
        dec.suite.add_statement(self)

    def display(self, indent):
        if self.fromlist == PyConst(None):
            name = self.name.name
            alias = self.alias.name
            if name == alias or name.startswith(alias + "."):
                indent.write("import {}", name)
            else:
                indent.write("import {} as {}", name, alias)
        elif self.fromlist == PyConst(('*',)):
            indent.write("from {}{} import *", ''.rjust(self.level.val,'.'),self.name.name)
        else:
            names = []
            for name, alias in zip(self.fromlist, self.aslist):
                if name == alias:
                    names.append(name)
                else:
                    names.append("{} as {}".format(name, alias))
            indent.write("from {}{} import {}", ''.rjust(self.level.val,'.'), self.name, ", ".join(names))


class ImportFrom:
    def __init__(self, name):
        self.name = name

    def store(self, dec, dest):
        imp = dec.stack.peek()
        assert isinstance(imp, ImportStatement)

        if imp.fromlist != PyConst(None):

            imp.aslist.append(dest.name)
        else:
            imp.alias = dest


class SimpleStatement(PyStatement):
    def __init__(self, val):
        assert val is not None
        self.val = val

    def display(self, indent):
        indent.write(self.val)

    def gen_display(self, seq=()):
        return " ".join((self.val,) + seq)


class IfStatement(PyStatement):
    def __init__(self, cond, true_suite, false_suite):
        self.cond = cond
        self.true_suite = true_suite
        self.false_suite = false_suite

    def display(self, indent, is_elif=False):
        ptn = "elif {}:" if is_elif else "if {}:"
        indent.write(ptn, self.cond)
        self.true_suite.display(indent + 1)
        if not self.false_suite:
            return
        if len(self.false_suite) == 1:
            stmt = self.false_suite[0]
            if isinstance(stmt, IfStatement):
                stmt.display(indent, is_elif=True)
                return
        indent.write("else:")
        self.false_suite.display(indent + 1)

    def gen_display(self, seq=()):
        s = 'if '
        if len(seq) >= 1:
            ss = seq[-1]
            if ss[-3:] == ' or' or ss[-4:] == ' and':
                s = ''
        if self.false_suite:
            # missed verifying SimpleStatement 
            if isinstance(self.true_suite.statements[0], IfStatement):
                s = s + "not {} or".format(self.cond)
                return self.true_suite.gen_display(seq + (s,))
            elif isinstance(self.false_suite.statements[0], IfStatement):
                s = s + "{} or".format(self.cond)
                return self.false_suite.gen_display(seq + (s,))
            else:
                raise Exception('unrecognized genexp')
        
        if isinstance(self.cond, PyIfElse):
            s = s + "({})".format(self.cond)
        else:
            s = s + "{}".format(self.cond)
        if isinstance(self.true_suite.statements[0], IfStatement):
            s = s + ' and'
        return self.true_suite.gen_display(seq + (s,))


class ForStatement(PyStatement, AsyncMixin):
    def __init__(self, iterable):
        AsyncMixin.__init__(self)
        self.iterable = iterable
        self.else_body: Suite = None

    def store(self, dec, dest):
        self.dest = dest

    def display(self, indent):
        indent.write("{}for {} in {}:", self.async_prefix, self.dest, self.iterable)
        self.body.display(indent + 1)
        if self.else_body:
            indent.write('else:')
            self.else_body.display(indent + 1)

    def gen_display(self, seq=()):
        s = "{}for {} in {}".format(self.async_prefix, self.dest, self.iterable.wrap() if isinstance(self.iterable, PyIfElse) else self.iterable)
        return self.body.gen_display(seq + (s,))


class WhileStatement(PyStatement):
    def __init__(self, cond, body):
        self.cond = cond
        self.body = body
        self.else_body: Suite = None

    def display(self, indent):
        indent.write("while {}:", self.cond)
        self.body.display(indent + 1)
        if self.else_body:
            indent.write('else:')
            self.else_body.display(indent + 1)


class DecorableStatement(PyStatement):
    def __init__(self):
        self.decorators = []

    def display(self, indent):
        indent.sep()
        for f in reversed(self.decorators):
            indent.write("@{}", f)
        self.display_undecorated(indent)
        indent.sep()

    def decorate(self, f):
        self.decorators.append(f)


class DefStatement(FunctionDefinition, DecorableStatement, AsyncMixin):
    def __init__(self, code: Code, defaults, kwdefaults, closure, paramobjs=None, annotations=None):
        FunctionDefinition.__init__(self, code, defaults, kwdefaults, closure, paramobjs, annotations)
        DecorableStatement.__init__(self)
        AsyncMixin.__init__(self)
        self.is_async = code.flags.coroutine or code.flags.async_generator
        self.code.annotations = self.annotations

    def display_undecorated(self, indent):
        paramlist = ", ".join(self.getparams())
        result = self.getreturn()
        if result:
            indent.write("{}def {}({}) -> {}:", self.async_prefix, self.code.name, paramlist, result)
        else:
            indent.write("{}def {}({}):", self.async_prefix, self.code.name, paramlist)
        # Assume that co_consts starts with None unless the function
        # has a docstring, in which case it starts with the docstring
        if self.code.consts[0] != PyConst(None):
            docstring = self.code.consts[0].val
            DocString(docstring).display(indent + 1)
        self.code.get_suite().display(indent + 1)

    def store(self, dec, dest):
        self.name = dest
        dec.suite.add_statement(self)


class TryStatement(PyStatement):
    def __init__(self, try_suite):
        self.try_suite: Suite = try_suite
        self.except_clauses: List[Any, str, Suite] = []
        self.else_suite: Suite = None

    def add_except_clause(self, exception_type, suite):
        self.except_clauses.append([exception_type, None, suite])

    def store(self, dec, dest):
        self.except_clauses[-1][1] = dest

    def display(self, indent):
        indent.write("try:")
        self.try_suite.display(indent + 1)
        for type, name, suite in self.except_clauses:
            if type is None:
                indent.write("except:")
            elif name is None:
                indent.write("except {}:", type)
            else:
                indent.write("except {} as {}:", type, name)
            suite.display(indent + 1)
        if self.else_suite:
            indent.write('else:')
            self.else_suite.display(indent + 1)


class FinallyStatement(PyStatement):
    def __init__(self, try_suite, finally_suite):
        self.try_suite = try_suite
        self.finally_suite = finally_suite

    def display(self, indent):
        # Wrap the try suite in a TryStatement if necessary
        try_stmt = None
        if len(self.try_suite) == 1:
            try_stmt = self.try_suite[0]
            if not isinstance(try_stmt, TryStatement):
                try_stmt = None
        if try_stmt is None:
            try_stmt = TryStatement(self.try_suite)
        try_stmt.display(indent)
        indent.write("finally:")
        self.finally_suite.display(indent + 1)


class WithStatement(PyStatement):
    def __init__(self, with_expr):
        self.with_expr = with_expr
        self.with_name = None
        self.is_async = False

    @property
    def async_prefix(self):
        return 'async ' if self.is_async else ''

    def store(self, dec, dest):
        self.with_name = dest

    def display(self, indent, args=None):
        # args to take care of nested withs:
        # with x as t:
        # with y as u:
        #         <suite>
        # --->
        # with x as t, y as u:
        #     <suite>
        if args is None:
            args = []
        if self.with_name is None:
            args.append(str(self.with_expr))
        else:
            args.append("{} as {}".format(self.with_expr, self.with_name))
        if len(self.suite) == 1 and isinstance(self.suite[0], WithStatement):
            self.suite[0].display(indent, args)
        else:
            indent.write(self.async_prefix + "with {}:", ", ".join(args))
            self.suite.display(indent + 1)


class ClassStatement(DecorableStatement):
    def __init__(self, func, name, parents, kwargs):
        DecorableStatement.__init__(self)
        self.func = func
        self.parents = parents
        self.kwargs = kwargs
        self.name = name

    def store(self, dec, dest):
        self.name = dest
        dec.suite.add_statement(self)

    def display_undecorated(self, indent):
        if self.parents or self.kwargs:
            args = [str(x) for x in self.parents]
            kwargs = ["{}={}".format(str(k).replace('\'', ''), v) for k, v in self.kwargs]
            all_args = ", ".join(args + kwargs)
            indent.write("class {}({}):", self.name, all_args)
        else:
            indent.write("class {}:", self.name)
        suite = self.func.code.get_suite(look_for_docstring=True)
        if suite:
            # TODO: find out why sometimes the class suite ends with
            # "return __class__"
            last_stmt = suite[-1]
            if isinstance(last_stmt, SimpleStatement):
                if last_stmt.val.startswith("return "):
                    suite.statements.pop()
            clean_vars = ['__module__', '__qualname__']
            for clean_var in clean_vars:
                for i in range(len(suite.statements)):
                    stmt = suite.statements[i]
                    if isinstance(stmt, AssignStatement) and str(stmt).startswith(clean_var):
                        suite.statements.pop(i)
                        break

        suite.display(indent + 1)


class Suite:
    def __init__(self):
        self.statements = []

    def __bool__(self) -> bool:
        return bool(self.statements)

    def __len__(self) -> int:
        return len(self.statements)

    def __getitem__(self, i) -> PyStatement:
        return self.statements[i]

    def __setitem__(self, i, val: PyStatement):
        self.statements[i] = val

    def __str__(self):
        istr = IndentString()
        self.display(istr)
        return str(istr)

    def display(self, indent):
        if self.statements:
            for stmt in self.statements:
                stmt.display(indent)
        else:
            indent.write("pass")

    def gen_display(self, seq=()):
        if len(self) != 1:
            raise Exception('There should only be one statement in a generator.')
        return self[0].gen_display(seq)

    def add_statement(self, stmt):
        self.statements.append(stmt)


class SuiteDecompiler:
    # An instruction handler can return this to indicate to the run()
    # function that it should return immediately
    END_NOW = object()

    # This is put on the stack by LOAD_BUILD_CLASS
    BUILD_CLASS = object()

    def __init__(self, start_addr: Address, end_addr: Address=None, stack=None):
        self.start_addr = start_addr
        self.end_addr = end_addr
        self.code: Code = start_addr.code
        self.stack = Stack() if stack is None else stack
        self.suite: Suite = Suite()
        self.assignment_chain = []
        self.popjump_stack = []
        self.scan_for_else = False
        self.find_end_finally = False
        self.expression_in_result = False
        if self.end_addr:
            self.end_block = self.end_addr[-1]
        else:
            self.end_block = Address(self.code, len(self.code.instr_seq)-1)

    def push_popjump(self, jtruthiness, jaddr, jcond, original_jaddr: Address):
        stack = self.popjump_stack
        if original_jaddr in self.code.end_chained_jumps:
            if jtruthiness:
                next_addr = original_jaddr[3]# not(a<b<c)
            else:
                next_addr = original_jaddr[4]#(a<b<c)
        else:
            next_addr = original_jaddr[1]
        while stack:
            truthiness, addr, cond, original_addr = stack[-1]
            allowCollision = False
            if jaddr == addr:
                stack.pop()
                if truthiness and jtruthiness:
                    obj_maker = PyBooleanOr
                elif truthiness and not jtruthiness:
                    if isinstance(cond, PyCompare) and cond.complist[1].startswith('is'):
                        obj_maker = PyBooleanOr
                        jcond = SPyNot(jcond)
                        jtruthiness = True
                    else:
                        obj_maker = PyBooleanAnd
                        cond = SPyNot(cond)
                        allowCollision = True
                elif not truthiness and jtruthiness:
                    if isinstance(jcond, PyCompare) and jcond.complist[1].startswith('is'):
                        obj_maker = PyBooleanOr
                        cond = SPyNot(cond)
                    else:
                        obj_maker = PyBooleanAnd
                        jcond = SPyNot(jcond)
                        jtruthiness = False
                        allowCollision = True
                else:
                    obj_maker = PyBooleanAnd
            elif addr == next_addr:
                stack.pop()
                if truthiness and jtruthiness:
                    if (isinstance(jcond, PyCompare) and jcond.complist[1].startswith('is')) or\
                            original_jaddr.opcode == JUMP_IF_TRUE_OR_POP:
                        obj_maker = PyBooleanAnd
                        cond = SPyNot(cond)
                    else:
                        obj_maker = PyBooleanOr
                        jcond = SPyNot(jcond)
                        jtruthiness = False
                        allowCollision = True
                elif truthiness and not jtruthiness:
                    obj_maker = PyBooleanOr
                elif not truthiness and jtruthiness:
                    obj_maker = PyBooleanAnd
                else:
                    if isinstance(cond, PyCompare) and cond.complist[1].startswith('is'):
                        obj_maker = PyBooleanAnd
                        jcond = SPyNot(jcond)
                        jtruthiness = True
                    else:
                        obj_maker = PyBooleanOr
                        cond = SPyNot(cond)
                        allowCollision = True
            else:
                break
            
            #last_true = original_addr.seek_back(POP_JUMP_IF_TRUE)
            #if isinstance(cond, PyBooleanOr)and obj_maker == PyBooleanAnd and (not last_true or last_true.jump() > original_jaddr):
                #jcond = PyBooleanOr(cond.left, obj_maker(cond.right, jcond))
            if isinstance(jcond, obj_maker):
                # Use associativity of 'and' and 'or' to minimise the
                # number of parentheses
                jcond = obj_maker(obj_maker(cond, jcond.left, allowCollision), jcond.right, allowCollision)
            else:
                jcond = obj_maker(cond, jcond, allowCollision)
        if original_jaddr.opcode == JUMP_IF_TRUE_OR_POP:
            jtruthiness = not jtruthiness
        stack.append((jtruthiness, jaddr, jcond, original_jaddr))

    def pop_popjump(self):
        if not self.popjump_stack:
            raise Exception('Attempted to pop an empty popjump stack.')
        truthiness, addr, cond, original_addr = self.popjump_stack.pop()
        if truthiness:
            cond = SPyNot(cond)
        return cond
    
    def popjump_stack_farthestaddr(self):
        stack = self.popjump_stack
        ra = None
        for truthiness, addr, cond, original_addr in stack:
            if (ra is None) or addr > ra:
                ra = addr
        if ra > self.end_block:
            ra = None
        return ra
    
    def popjump_stack_ifpassgetcond(self, jtruthiness, jaddr, jcond, original_jaddr: Address, nextaddr, farthestaddr):
        stack = self.popjump_stack
        l = len(stack)
        for i in range(l):
            a = stack[i]
            if a[1] == farthestaddr:
                stack[i] = (a[0], nextaddr, a[2], a[3])
        self.push_popjump(jtruthiness, jaddr, jcond, original_jaddr)
        return self.pop_popjump()

    def pop_condition_popjump(self):
        if self.popjump_stack:
            truthiness, addr, cond, original_addr = self.popjump_stack[-1]
            if isinstance(cond, PyCompare):
                truthiness, addr, cond, original_addr = self.popjump_stack.pop()
                return cond
        return None
    
    def run(self):
        addr = self.start_addr
        while addr and addr < self.end_addr:
            opcode, arg = addr
            args = (addr,) if opcode < HAVE_ARGUMENT else (addr, arg)
            if self.scan_for_else:
                if addr.opcode == JUMP_ABSOLUTE and addr.addr not in self.code.linemap:
                    break
                elif addr.opcode == JUMP_FORWARD:
                    break
            elif self.find_end_finally and addr.opcode == END_FINALLY:
                break
            method = getattr(self, opname[opcode])
            new_addr = method(*args)
            if new_addr is self.END_NOW:
                addr = self.end_addr
                break
            elif new_addr is None:
                new_addr = addr[1]
            if (self.scan_for_else or self.find_end_finally) and addr.opcode == RETURN_VALUE:
                break
            addr = new_addr
            if self.expression_in_result and addr.opcode == RETURN_VALUE:
                break
        return addr

    def write(self, template, *args):
        def fmt(x):
            if isinstance(x, int):
                return self.stack.getval(x)
            else:
                return x

        if args:
            line = template.format(*map(fmt, args))
        else:
            line = template
        self.suite.add_statement(SimpleStatement(line))

    def store(self, dest):
        val = self.stack.pop()
        val.store(self, dest)

    def is_for_loop(self, addr, end_addr):
        i = 0
        while 1:
            cur_addr = addr[i]
            if cur_addr == end_addr:
                break
            elif cur_addr.opcode in else_jump_opcodes:
                cur_addr = cur_addr.jump()
                if cur_addr and cur_addr.opcode in for_jump_opcodes:
                    return True
                break
            elif cur_addr.opcode in for_jump_opcodes:
                return True
            i = i + 1
        return False
    
    def instructions_after(self, addr):
        if addr:
            addr = addr[1]
            return addr and addr.seek_stmt(None)
        return False
    
    def verify_loop_laststmt(self, ss: Suite, startaddr, endaddr):
        if len(ss.statements):
            stmt = ss.statements[-1]
            if isinstance(stmt, SimpleStatement) and self.end_block.is_continue_jump:
                if stmt.val == "continue" and self.end_block.addr in self.code.linemap:
                    stmt.val = "pass"
                elif stmt.val.startswith("return") and self.end_block.addr not in self.code.linemap:
                    for addr in self.code.qcjumps:
                        if startaddr <= addr < endaddr:
                            self.code.statement_jumps.append(addr)
                            self.code.qcjumps.remove(addr)
                            return False
        return True
    
    #
    # All opcode methods in CAPS below.
    #

    def SETUP_LOOP(self, addr: Address, delta):
        for wcontext in self.code.loops:
            if wcontext[0] == addr.index:
                break
        if wcontext[1] < 0:#is_for_loop
            return
        jump_addr = self.code[wcontext[2]] #addr.jump()
        end_addr = jump_addr[-1]
        if wcontext[1] > 0:
            end_cond = self.code[wcontext[1]]
            end_cond_j = end_cond.jump()
            while True:
                d_body = SuiteDecompiler(addr[1], end_cond_j)
                d_body.end_while_condition = end_cond
                d_body.run()
                result = d_body.suite.statements.pop()
                if isinstance(result, IfStatement):
                    if d_body.verify_loop_laststmt(result.true_suite, end_cond, end_cond_j):
                        break
                else:
                    self.suite.add_statement(SimpleStatement('"""unpyc3: decompilation error: while condition isnt IfStatement"""'))
                    d_body.verify_loop_laststmt(d_body.suite, end_cond, end_cond_j)
                    while_stmt = WhileStatement(PyName('unrecognised'), d_body.suite)
                    if(end_cond_j.opcode == POP_BLOCK):
                        d_else = SuiteDecompiler(end_cond_j[1],jump_addr)
                        d_else.run()
                        while_stmt.else_body = d_else.suite
                    self.suite.add_statement(while_stmt)
                    self.suite.add_statement(SimpleStatement('"""unpyc3: ------------------------"""'))
                    return jump_addr
            while_stmt = WhileStatement(result.cond, result.true_suite)
            if(end_cond_j.opcode == POP_BLOCK):
                d_else = SuiteDecompiler(end_cond_j[1],jump_addr)
                d_else.run()
                while_stmt.else_body = d_else.suite
            self.suite.add_statement(while_stmt)
        else:
            while True:
                d_body = SuiteDecompiler(addr[1], end_addr)
                d_body.end_while_condition = addr
                d_body.run()
                if d_body.verify_loop_laststmt(d_body.suite, addr[1], end_addr):
                    break
            while_stmt = WhileStatement(PyConst(True), d_body.suite)
            self.suite.add_statement(while_stmt)
        return jump_addr

    def BREAK_LOOP(self, addr):
        self.write("break")

    def CONTINUE_LOOP(self, addr, *argv):
        self.write("continue")

    def SETUP_FINALLY(self, addr, delta):
        start_finally: Address = addr.jump()
        d_try = SuiteDecompiler(addr[1], start_finally)
        d_try.run()
        d_finally = SuiteDecompiler(start_finally)
        d_finally.find_end_finally = True
        end_finally = d_finally.run()
        self.suite.add_statement(FinallyStatement(d_try.suite, d_finally.suite))
        if end_finally and end_finally.opcode == END_FINALLY:
            return end_finally[1]
        else:
            return self.END_NOW

    def END_FINALLY(self, addr):
        return self.END_NOW

    def SETUP_EXCEPT(self, addr, delta):
        end_addr = addr
        start_except = addr.jump()
        start_try = addr[1]
        end_try = start_except
        if sys.version_info < (3, 7):
            if end_try.opcode == JUMP_FORWARD:
                end_try = end_try[1] + end_try.arg
            elif end_try.opcode == JUMP_ABSOLUTE:
                end_try = end_try[-1]
            else:
                end_try = end_try[1]
        d_try = SuiteDecompiler(start_try, end_try)
        d_try.run()

        stmt = TryStatement(d_try.suite)
        fend = self.end_block
        if end_try[-1].opcode == JUMP_FORWARD:
            fend = end_try[-1].jump()
        j_except: Address = None
        while start_except.opcode != END_FINALLY:
            if start_except.opcode == DUP_TOP:
                # There's a new except clause
                d_except = SuiteDecompiler(start_except[1])
                d_except.stack.push(stmt)
                d_except.run()
                start_except = stmt.next_start_except
                if j_except is None or start_except[-1].opcode == JUMP_FORWARD or\
                        j_except.opcode == RETURN_VALUE:
                    j_except = start_except[-1]
                end_addr = start_except[1]
            elif start_except.opcode == POP_TOP:
                # It's a bare except clause - it starts:
                # POP_TOP
                # POP_TOP
                # POP_TOP
                # <except stuff>
                # POP_EXCEPT
                start_except = start_except[3]
                end_except = start_except

                while end_except < fend:
                    if end_except.opcode == SETUP_EXCEPT:
                        nested_try = SuiteDecompiler(end_except, fend)
                        nested_try = nested_try.SETUP_EXCEPT(end_except, end_except.arg)
                    elif end_except.opcode == POP_EXCEPT:
                        break
                    end_except = end_except[1]
                if end_except.opcode == POP_EXCEPT:
                    d_except = SuiteDecompiler(start_except, end_except)
                    d_except.run()
                    stmt.add_except_clause(None, d_except.suite)
                    start_except = end_except[2]
                    assert start_except.opcode == END_FINALLY
                    end_addr = start_except[1]
                    if j_except is None or end_except[1].opcode == JUMP_FORWARD or\
                            j_except.opcode == RETURN_VALUE:
                        j_except: Address = end_except[1]
                else:# Handle edge case where there is a return in the except
                    if j_except and j_except.opcode == JUMP_FORWARD:
                        if end_try[-1].opcode == JUMP_FORWARD:
                            d_except = SuiteDecompiler(start_except, fend)
                            d_except.run()
                            stmt.add_except_clause(None, d_except.suite)
                            end_addr = j_except.jump()
                            d_else = SuiteDecompiler(fend, end_addr)
                            d_else.run()
                            stmt.else_suite = d_else.suite
                            self.suite.add_statement(stmt)
                            return end_addr
                        else:
                            end_addr = j_except.jump()
                            d_except = SuiteDecompiler(start_except, end_addr)
                            d_except.run()
                            x = len(d_except.suite.statements)
                            i = 0
                            end_except = -1
                            while i < x:
                                stmt = d_except.suite.statements[i]
                                if isinstance(stmt, SimpleStatement):
                                    if stmt.val.startswith("return"):
                                        end_except = i
                                        break
                                i = i + 1
                            assert i >= 0
                            if end_except + 1 < x:
                                end_except = end_except + 1
                                stmt.else_suite = Suite()
                                stmt.else_suite.statements = d_except.suite.statements[end_except:]
                                d_except.suite.statements = d_except.suite.statements[:end_except]
                            stmt.add_except_clause(None, d_except.suite)
                            self.suite.add_statement(stmt)
                            return end_addr
                    else:
                        if fend == self.end_block:
                            fend = self.end_addr
                        d_except = SuiteDecompiler(start_except, fend)
                        d_except.scan_for_else = True
                        end_except = d_except.run()
                        assert end_except.opcode == RETURN_VALUE
                        stmt.add_except_clause(None, d_except.suite)
                        self.suite.add_statement(stmt)
                        return end_except[1]
        
        self.suite.add_statement(stmt)

        has_normal_else_clause = j_except.opcode == JUMP_FORWARD and j_except[2] != j_except.jump()
        has_end_of_loop_else_clause = j_except.opcode == JUMP_ABSOLUTE and j_except.is_continue_jump and j_except[1].opcode == END_FINALLY
        has_return_else_clause = j_except.opcode == RETURN_VALUE
        if has_normal_else_clause or has_end_of_loop_else_clause or has_return_else_clause:
            assert j_except[1].opcode == END_FINALLY
            start_else = j_except[2]
            if has_return_else_clause and start_else.opcode == JUMP_ABSOLUTE and start_else[1].opcode == POP_BLOCK:
                start_else = start_else[-1]
            end_else: Address = None
            if has_normal_else_clause:
                end_else = j_except.jump()
            elif has_end_of_loop_else_clause:
                end_try = end_try[-1]
                if end_try.is_continue_jump:
                    return end_addr
                end_else = self.end_addr
            elif has_return_else_clause:
                return end_addr
                #end_else = j_except[1].seek_forward(RETURN_VALUE)[1]
            #if has_return_else_clause and not end_else:
                #return end_addr
            d_else = SuiteDecompiler(start_else, end_else)
            d_else.run()
            end_addr = end_else
            stmt.else_suite = d_else.suite
        return end_addr

    def SETUP_WITH(self, addr, delta):
        end_with = addr.jump()
        with_stmt = WithStatement(self.stack.pop())
        d_with = SuiteDecompiler(addr[1], end_with)
        d_with.stack.push(with_stmt)
        d_with.run()
        with_stmt.suite = d_with.suite
        self.suite.add_statement(with_stmt)
        if sys.version_info <= (3, 4):
            assert end_with.opcode == WITH_CLEANUP
            assert end_with[1].opcode == END_FINALLY
            return end_with[2]
        else:
            assert end_with.opcode == WITH_CLEANUP_START
            assert end_with[1].opcode == WITH_CLEANUP_FINISH
            return end_with[3]

    def POP_BLOCK(self, addr):
        pass

    def POP_EXCEPT(self, addr):
        return self.END_NOW

    def NOP(self, addr):
        return

    def SETUP_ANNOTATIONS(self, addr):
        self.code.annotationd = True
        return

    def COMPARE_OP(self, addr, compare_opname):
        left, right = self.stack.pop(2)
        if compare_opname != 10:  # 10 is exception match
            self.stack.push(PyCompare([left, cmp_op[compare_opname], right]))
        else:
            # It's an exception match
            # left is a TryStatement
            # right is the exception type to be matched
            # It goes:
            # COMPARE_OP 10
            # POP_JUMP_IF_FALSE <next except>
            # POP_TOP
            # POP_TOP or STORE_FAST (if the match is named)
            # POP_TOP
            # SETUP_FINALLY if the match was named
            assert addr[1].opcode == POP_JUMP_IF_FALSE
            left.next_start_except = addr[1].jump()
            assert addr[2].opcode == POP_TOP
            assert addr[4].opcode == POP_TOP
            if addr[5].opcode == SETUP_FINALLY:
                except_start = addr[6]
                except_end = addr[5].jump()
            else:
                except_start = addr[5]
                except_end = left.next_start_except
            d_body = SuiteDecompiler(except_start, except_end)
            d_body.run()
            left.add_except_clause(right, d_body.suite)
            if addr[3].opcode != POP_TOP:
                # The exception is named
                d_exc_name = SuiteDecompiler(addr[3], addr[4])
                d_exc_name.stack.push(left)
                # This will store the name in left:
                d_exc_name.run()
            # We're done with this except clause
            return self.END_NOW

    def PRINT_EXPR(self, addr):
        expr = self.stack.pop()
        self.write("{}", expr)
    
    #
    # Stack manipulation
    #

    def POP_TOP(self, addr):
        if len(self.stack) <= 0:
            self.suite.add_statement(SimpleStatement('"""unpyc3: decompilation error POP_TOP while empty stack"""'))
            return
        self.stack.pop().on_pop(self)

    def ROT_TWO(self, addr: Address):
        # special case: x, y = z, t

        if addr[-1].opcode in (LOAD_ATTR, LOAD_GLOBAL, LOAD_NAME, LOAD_CONST, LOAD_FAST, LOAD_DEREF, BINARY_SUBSCR, BUILD_LIST, CALL_FUNCTION):
            next_stmt = addr.seek_forward((*unpack_terminators, *pop_jump_if_opcodes, *else_jump_opcodes))
            if next_stmt is None or next_stmt > self.end_block:
                next_stmt = self.end_addr
            first = addr.seek_forward(unpack_stmt_opcodes, next_stmt)
            second = first and first.seek_forward(unpack_stmt_opcodes, next_stmt)
            if first and second and len({*[first.opcode, second.opcode]}) == 1:
                val = PyTuple(self.stack.pop(2))
                unpack = Unpack(val, 2)
                self.stack.push(unpack)
                self.stack.push(unpack)
                return

        tos1, tos = self.stack.pop(2)
        self.stack.push(tos, tos1)

    def ROT_THREE(self, addr: Address):
        if not (addr[-1].opcode == DUP_TOP and addr[1].opcode == COMPARE_OP and\
                addr[2].opcode in (JUMP_IF_FALSE_OR_POP, POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE)):
            # special case: x, y, z = a, b, c
            next_stmt = addr.seek_forward((*unpack_terminators, *pop_jump_if_opcodes, *else_jump_opcodes))
            if next_stmt is None or next_stmt > self.end_block:
                next_stmt = self.end_addr
            rot_two = addr[1]
            first = rot_two and rot_two.seek_forward(unpack_stmt_opcodes, next_stmt)
            second = first and first.seek_forward(unpack_stmt_opcodes, next_stmt)
            third = second and second.seek_forward(unpack_stmt_opcodes, next_stmt)
            if first and second and third and len({*[first.opcode, second.opcode,third.opcode]}) == 1:
                val = PyTuple(self.stack.pop(3))
                unpack = Unpack(val, 3)
                self.stack.push(unpack)
                self.stack.push(unpack)
                self.stack.push(unpack)
                return addr[2]
        
        tos2, tos1, tos = self.stack.pop(3)
        self.stack.push(tos, tos2, tos1)

    def DUP_TOP(self, addr):
        self.stack.push(self.stack.peek())

    def DUP_TOP_TWO(self, addr):
        self.stack.push(*self.stack.peek(2))

    #
    # LOAD / STORE / DELETE
    #

    # FAST

    def LOAD_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.stack.push(name)

    def STORE_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.store(name)

    def DELETE_FAST(self, addr, var_num):
        name = self.code.varnames[var_num]
        self.write("del {}", name)

    # DEREF

    def LOAD_DEREF(self, addr, i):
        name = self.code.derefnames[i]
        self.stack.push(name)

    def LOAD_CLASSDEREF(self, addr, i):
        name = self.code.derefnames[i]
        self.stack.push(name)

    def STORE_DEREF(self, addr, i):
        name = self.code.derefnames[i]
        if not self.code.iscellvar(i):
            self.code.declare_nonlocal(name)
        self.store(name)

    def DELETE_DEREF(self, addr, i):
        name = self.code.derefnames[i]
        if not self.code.iscellvar(i):
            self.code.declare_nonlocal(name)
        self.write("del {}", name)

    # GLOBAL

    def LOAD_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.code.ensure_global(name)
        self.stack.push(name)

    def STORE_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.code.declare_global(name)
        self.store(name)

    def DELETE_GLOBAL(self, addr, namei):
        name = self.code.names[namei]
        self.declare_global(name)
        self.write("del {}", name)

    # NAME

    def LOAD_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.stack.push(name)

    def STORE_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.store(name)

    def DELETE_NAME(self, addr, namei):
        name = self.code.names[namei]
        self.write("del {}", name)

    # METHOD
    def LOAD_METHOD(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.stack.push(PyAttribute(expr, attrname))

    def CALL_METHOD(self, addr, argc, have_var=False, have_kw=False):
        kw_argc = argc >> 8
        pos_argc = argc
        varkw = self.stack.pop() if have_kw else None
        varargs = self.stack.pop() if have_var else None
        kwargs_iter = iter(self.stack.pop(2 * kw_argc))
        kwargs = list(zip(kwargs_iter, kwargs_iter))
        posargs = self.stack.pop(pos_argc)
        func = self.stack.pop()
        if func is self.BUILD_CLASS:
            # It's a class construction
            # TODO: check the assert statement below is correct
            assert not (have_var or have_kw)
            func, name, *parents = posargs
            self.stack.push(ClassStatement(func, name, parents, kwargs))
        elif isinstance(func, PyComp):
            # It's a list/set/dict comprehension or generator expression
            assert not (have_var or have_kw)
            assert len(posargs) == 1 and not kwargs
            func.set_iterable(posargs[0])
            self.stack.push(func)
        elif posargs and isinstance(posargs[0], DecorableStatement):
            # It's a decorator for a def/class statement
            assert len(posargs) == 1 and not kwargs
            defn = posargs[0]
            defn.decorate(func)
            self.stack.push(defn)
        else:
            # It's none of the above, so it must be a normal function call
            func_call = PyCallFunction(func, posargs, kwargs, varargs, varkw)
            self.stack.push(func_call)

    # ATTR

    def LOAD_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.stack.push(PyAttribute(expr, attrname))

    def STORE_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.store(PyAttribute(expr, attrname))

    def DELETE_ATTR(self, addr, namei):
        expr = self.stack.pop()
        attrname = self.code.names[namei]
        self.write("del {}.{}", expr, attrname)

    def STORE_SUBSCR(self, addr):
        expr, sub = self.stack.pop(2)
        if self.code.annotationd and isinstance(sub,PyConst) and isinstance(expr,PyName) and expr.name == '__annotations__':
            an = self.stack.pop()
            if isinstance(an, PyConst) and isinstance(an.val, str):
                an = an.val
            else:
                an = str(an)
            newname = sub.val + ': ' + an
            if len(self.suite) >= 1:
                lastst = self.suite[-1]
                if isinstance(lastst, AssignStatement):
                    sname = lastst.chain[0]
                    if isinstance(sname, PyName) and sname.name == sub.val:
                        lastst.chain[0] = PyName(newname)
                        return
            self.suite.add_statement(SimpleStatement(newname))
            return
        self.store(PySubscript(expr, sub))

    def DELETE_SUBSCR(self, addr):
        expr, sub = self.stack.pop(2)
        self.write("del {}[{}]", expr, sub)

    # CONST
    CONST_LITERALS = {
        Ellipsis: PyName('...')
    }
    def LOAD_CONST(self, addr, consti):
        const = self.code.consts[consti]
        if const.val in self.CONST_LITERALS:
            const = self.CONST_LITERALS[const.val]
        self.stack.push(const)

    #
    # Import statements
    #

    def IMPORT_NAME(self, addr, namei):
        name = self.code.names[namei]
        level, fromlist = self.stack.pop(2)
        self.stack.push(ImportStatement(name, level, fromlist))
        # special case check for import x.y.z as w syntax which uses
        # attributes and assignments and is difficult to workaround
        i = 1
        while addr[i].opcode == LOAD_ATTR: i = i + 1
        if i > 1 and addr[i].opcode in (STORE_FAST, STORE_NAME, STORE_DEREF):
            return addr[i]
        return None

    def IMPORT_FROM(self, addr: Address, namei):
        name = self.code.names[namei]
        self.stack.push(ImportFrom(name))
        if addr[1].opcode == ROT_TWO:
            return addr.seek_forward((STORE_NAME, STORE_FAST, STORE_DEREF))
        return None

    def IMPORT_STAR(self, addr):
        self.POP_TOP(addr)

    #
    # Function call
    #

    def STORE_LOCALS(self, addr):
        self.stack.pop()
        return addr[3]

    def LOAD_BUILD_CLASS(self, addr):
        self.stack.push(self.BUILD_CLASS)

    def RETURN_VALUE(self, addr):
        value = self.stack.pop()
        if self.code.flags.generator and isinstance(value, PyConst) and value.val is None and not addr[-2]:
            cond = PyConst(False)
            body = SimpleStatement('yield None')
            loop = WhileStatement(cond, body)
            self.suite.add_statement(loop)
            return
        
        if isinstance(value, PyConst) and value.val is None:
            value = self.code.flags.generator and not self.code[0].seek_forward({YIELD_FROM, YIELD_VALUE})
            if addr[1] is not None or self.find_end_finally:
                self.write("return")
                if value:
                    #and addr[3]
                    self.write('yield')
            else:
                if value and not self.code[0].seek_forward(RETURN_VALUE, addr):
                    self.write("return")
                    self.write('yield')
            return
        if self.code.flags.iterable_coroutine:
            self.write("yield {}", value)
        else:
            self.write("return {}", value)
            if self.code.flags.generator:
                self.write('yield')

    def GET_YIELD_FROM_ITER(self, addr):
        pass

    def YIELD_VALUE(self, addr):
        if self.code.name == '<genexpr>':
            return
        value = self.stack.pop()
        self.stack.push(PyYield(value))

    def YIELD_FROM(self, addr):
        value = self.stack.pop()  # TODO:  from statement ?
        value = self.stack.pop()
        self.stack.push(PyYieldFrom(value))

    def CALL_FUNCTION_CORE(self, func, posargs, kwargs, varargs, varkw):
        if func is self.BUILD_CLASS:
            # It's a class construction
            # TODO: check the assert statement below is correct
            # assert not (have_var or have_kw)
            func, name, *parents = posargs
            self.stack.push(ClassStatement(func, name, parents, kwargs))
        elif isinstance(func, PyComp):
            # It's a list/set/dict comprehension or generator expression
            # assert not (have_var or have_kw)
            assert len(posargs) == 1 and not kwargs
            func.set_iterable(posargs[0])
            self.stack.push(func)
        elif posargs and isinstance(posargs, list) and isinstance(posargs[0], DecorableStatement):
            # It's a decorator for a def/class statement
            assert len(posargs) == 1 and not kwargs
            defn = posargs[0]
            defn.decorate(func)
            self.stack.push(defn)
        else:
            # It's none of the above, so it must be a normal function call
            func_call = PyCallFunction(func, posargs, kwargs, varargs, varkw)
            self.stack.push(func_call)

    def CALL_FUNCTION(self, addr, argc, have_var=False, have_kw=False):
        if sys.version_info >= (3, 6):
            pos_argc = argc
            posargs = self.stack.pop(pos_argc)
            func = self.stack.pop()
            self.CALL_FUNCTION_CORE(func, posargs, [], None, None)
        else:
            kw_argc = argc >> 8
            pos_argc = argc & 0xFF
            varkw = self.stack.pop() if have_kw else None
            varargs = self.stack.pop() if have_var else None
            kwargs_iter = iter(self.stack.pop(2 * kw_argc))
            kwargs = list(zip(kwargs_iter, kwargs_iter))
            posargs = self.stack.pop(pos_argc)
            func = self.stack.pop()
            self.CALL_FUNCTION_CORE(func, posargs, kwargs, varargs, varkw)

    def CALL_FUNCTION_VAR(self, addr, argc):
        self.CALL_FUNCTION(addr, argc, have_var=True)

    def CALL_FUNCTION_KW(self, addr, argc):
        if sys.version_info >= (3, 6):
            keys = self.stack.pop()
            kwargc = len(keys.val)
            kwarg_values = self.stack.pop(kwargc)
            posargs = self.stack.pop(argc - kwargc)
            func = self.stack.pop()
            kwarg_dict = list(zip([PyName(k) for k in keys], kwarg_values))
            self.CALL_FUNCTION_CORE(func, posargs, kwarg_dict, None, None)
        else:
            self.CALL_FUNCTION(addr, argc, have_kw=True)

    def CALL_FUNCTION_EX(self, addr, flags):
        kwarg_unpacks = []
        if flags & 1:
            kwarg_unpacks = self.stack.pop()

        kwarg_dict = PyDict()
        if isinstance(kwarg_unpacks,PyDict):
            kwarg_dict = kwarg_unpacks
            kwarg_unpacks = []
        elif isinstance(kwarg_unpacks, list):
            if len(kwarg_unpacks):
                if isinstance(kwarg_unpacks[0], PyDict):
                    kwarg_dict = kwarg_unpacks[0]
                    kwarg_unpacks = kwarg_unpacks[1:]
        else:
            kwarg_unpacks = [kwarg_unpacks]

        if any(filter(lambda kv: '.' in str(kv[0]), kwarg_dict.items)):
            kwarg_unpacks.append(kwarg_dict)
            kwarg_dict = PyDict()

        posargs_unpacks = self.stack.pop()
        posargs = PyTuple([])
        if isinstance(posargs_unpacks,PyTuple):
            posargs = posargs_unpacks
            posargs_unpacks = []
        elif isinstance(posargs_unpacks, list):
            if len(posargs_unpacks) > 0:
                if isinstance(posargs_unpacks[0], PyTuple):
                    posargs = posargs_unpacks[0]
                    posargs_unpacks = posargs_unpacks[1:]
                elif isinstance(posargs_unpacks[0], PyConst) and isinstance(posargs_unpacks[0].val, tuple):
                    posargs = PyTuple(list(map(PyConst,posargs_unpacks[0].val)))
                    posargs_unpacks = posargs_unpacks[1:]

        else:
            posargs_unpacks = [posargs_unpacks]

        func = self.stack.pop()
        self.CALL_FUNCTION_CORE(func, list(posargs.values), list(kwarg_dict.items), posargs_unpacks, kwarg_unpacks)

    def CALL_FUNCTION_VAR_KW(self, addr, argc):
        self.CALL_FUNCTION(addr, argc, have_var=True, have_kw=True)

    # a, b, ... = ...

    def UNPACK_SEQUENCE(self, addr, count):
        unpack = Unpack(self.stack.pop(), count)
        for i in range(count):
            self.stack.push(unpack)

    def UNPACK_EX(self, addr, counts):
        rcount = counts >> 8
        lcount = counts & 0xFF
        count = lcount + rcount + 1
        unpack = Unpack(self.stack.pop(), count, lcount)
        for i in range(count):
            self.stack.push(unpack)

    # Build operations

    def BUILD_SLICE(self, addr, argc):
        assert argc in (2, 3)
        self.stack.push(PySlice(self.stack.pop(argc)))

    def BUILD_TUPLE(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PyTuple(values))

    def BUILD_TUPLE_UNPACK(self, addr, count):
        values = []
        for o in self.stack.pop(count):
            if isinstance(o, PyTuple):
                values.extend(o.values)
            else:
                values.append(PyStarred(o))

        self.stack.push(PyTuple(values))

    def BUILD_TUPLE_UNPACK_WITH_CALL(self, addr, count):
        self.stack.push(self.stack.pop(count))

    def BUILD_LIST(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PyList(values))

    def BUILD_LIST_UNPACK(self, addr, count):
        values = []
        for o in self.stack.pop(count):
            if isinstance(o, PyTuple):
                values.extend(o.values)
            else:
                values.append(PyStarred(o))

        self.stack.push(PyList(values))

    def BUILD_SET(self, addr, count):
        values = [self.stack.pop() for i in range(count)]
        values.reverse()
        self.stack.push(PySet(values))

    def BUILD_SET_UNPACK(self, addr, count):
        values = []
        for o in self.stack.pop(count):
            if isinstance(o, PySet):
                values.extend(o.values)
            else:
                values.append(PyStarred(o))

        self.stack.push(PySet(values))

    def BUILD_MAP(self, addr, count):
        d = PyDict()
        if sys.version_info >= (3, 5):
            for i in range(count):
                d.items.append(tuple(self.stack.pop(2)))
            d.items = list(reversed(d.items))
        self.stack.push(d)

    def BUILD_MAP_UNPACK(self, addr, count):
        d = PyDict()
        for i in range(count):
            o = self.stack.pop()
            if isinstance(o, PyDict):
                for item in reversed(o.items):
                    k, v = item
                    d.set_item(PyConst(k.val if isinstance(k, PyConst) else k.name), v)
            else:
                d.items.append((PyStarred(PyStarred(o)),))
        d.items = list(reversed(d.items))
        self.stack.push(d)

    def BUILD_MAP_UNPACK_WITH_CALL(self, addr, count):
        self.stack.push(self.stack.pop(count))

    def BUILD_CONST_KEY_MAP(self, addr, count):
        keys = self.stack.pop()
        vals = self.stack.pop(count)
        dict = PyDict()
        for i in range(count):
            dict.set_item(PyConst(keys.val[i]), vals[i])
        self.stack.push(dict)

    def STORE_MAP(self, addr):
        v, k = self.stack.pop(2)
        d = self.stack.peek()
        d.set_item(k, v)

    # Comprehension operations - just create an expression statement

    def LIST_APPEND(self, addr, i):
        self.POP_TOP(addr)

    def SET_ADD(self, addr, i):
        self.POP_TOP(addr)

    def MAP_ADD(self, addr, i):
        value, key = self.stack.pop(2)
        self.stack.push(PyKeyValue(key, value))
        self.POP_TOP(addr)

    # and operator

    def JUMP_IF_FALSE_OR_POP(self, addr: Address, target):
        end_addr = addr.jump()
        if addr[-3] and \
                addr[-1].opcode == COMPARE_OP and \
                addr[-2].opcode == ROT_THREE and \
                addr[-3].opcode == DUP_TOP:
            curaddr = addr[1]
            start_addr = curaddr
            cond = self.stack.pop()
            while curaddr < end_addr:
                if curaddr.opcode == COMPARE_OP:
                    if curaddr[-2].opcode == DUP_TOP and curaddr[-1].opcode == ROT_THREE and\
                            curaddr[1].opcode == JUMP_IF_FALSE_OR_POP and curaddr[1].arg == addr.arg:
                        d = SuiteDecompiler(start_addr, curaddr[1], self.stack)
                        d.run()
                        c = d.stack.pop()
                        cond = cond.chain(c)
                        start_addr = curaddr[2]
                curaddr = curaddr[1]
            d = SuiteDecompiler(start_addr, end_addr[-1], self.stack)
            d.run()
            c = d.stack.pop()
            cond = cond.chain(c)
            self.stack.push(cond)
            return end_addr[2]
        
        self.push_popjump(False, end_addr, self.stack.pop(), addr)
        left = self.pop_popjump()
        d = SuiteDecompiler(addr[1], end_addr, self.stack)
        d.run()
        # if end_addr.opcode == RETURN_VALUE:
        #     return end_addr[2]
        right = self.stack.pop()
        py_and = PyBooleanAnd(left, right)
        self.stack.push(py_and)
        return end_addr

    # This appears when there are chained comparisons, e.g. 1 <= x < 10

    def JUMP_FORWARD(self, addr, delta):

        return addr.jump()

    # or operator

    def JUMP_IF_TRUE_OR_POP(self, addr, target):
        end_addr = addr.jump()
        self.push_popjump(True, end_addr, self.stack.pop(), addr)
        left = self.pop_popjump()
        d = SuiteDecompiler(addr[1], end_addr, self.stack)
        d.run()
        right = self.stack.pop()
        self.stack.push(PyBooleanOr(left, right))
        return end_addr

    #
    # If-else statements/expressions and related structures
    #

    def POP_JUMP_IF(self, addr: Address, target: int, truthiness: bool) -> Union[Address, None]:
        jump_addr = addr.jump()
        next_addr = addr[1]
        j_addr = jump_addr
        
        if j_addr == next_addr:
            self.push_popjump(truthiness, jump_addr, self.stack.pop(), addr)
            cond = self.pop_popjump()
            d_true = Suite()
            d_true.add_statement(SimpleStatement('pass'))
            self.suite.add_statement(IfStatement(cond, d_true, None))
            return next_addr
        
        wcontext = addr.last_loop_context()
        last_loop = self.code[wcontext[0]]
        in_loop = last_loop != None
        is_loop_condition = wcontext[1] > 0 and addr.index <= wcontext[1]
        

        if addr.is_continue_jump:
            c = None
            if addr not in self.code.statement_jumps:
                for x in self.code.statement_jumps:
                    if addr < x:
                        if c is None or x < c:
                            c = x
                assert c and c < self.end_block
                if c in self.code.end_chained_jumps:
                    if c.opcode == POP_JUMP_IF_TRUE:
                        x = c[3]
                    else:
                        x = c[4]
                else:
                    x = c[1]
            if c and x.is_continue_jump and\
                    (truthiness or not c.is_continue_jump):
                jump_addr = x
            else:
                if jump_addr.opcode == FOR_ITER:
                    # We are in a for-loop with nothing after the if-suite
                    # But take care: for-loops in generator expression do
                    # not end in POP_BLOCK, hence the test below.
                    jump_addr = jump_addr.jump()
                elif jump_addr[-1].opcode == SETUP_LOOP:
                    # We are in a while-loop with nothing after the if-suite
                    jump_addr = jump_addr[-1].jump()[-1]
        
        cond = self.stack.pop()
        # chained compare
        # ex:
        # if x <= y <= z:
        if addr in self.code.start_chained_jumps:
            self.push_popjump(truthiness, jump_addr, cond, addr)
            return
        elif addr in self.code.inner_chained_jumps:
            c = self.pop_popjump()
            cond = c.chain(cond)
            self.push_popjump(False, jump_addr, cond, addr)
            return
        elif addr in self.code.end_chained_jumps:
            c = self.pop_popjump()
            cond = c.chain(cond)
        
        if addr in self.code.ternaryop_jumps:
            x = jump_addr[-1]
            self.push_popjump(truthiness, jump_addr, cond, addr)
            cond = self.pop_popjump()
            if x.opcode == RETURN_VALUE:# return (a if b else c)
                d_true = SuiteDecompiler(next_addr, x)
                d_true.run()
                true_expr = d_true.stack.pop()
                end_false = self.end_block
                if not end_false.opcode == RETURN_VALUE:
                    end_false = self.end_block.seek_back(RETURN_VALUE, jump_addr)#include nested ternary operators
                    if end_false is None:
                        end_false = self.end_block
                        if end_false[1] and end_false[1].opcode == RETURN_VALUE:# return d or (a if b else c)
                            end_false = end_false[1]
                        #else:
                            #Exception
                d_false = SuiteDecompiler(jump_addr, end_false)
                d_false.expression_in_result = True
                x = d_false.run()
                false_expr = d_false.stack.pop()
                if x and (end_false is None or x < end_false):
                    end_false = x
                cond = PyIfElse(cond, true_expr, false_expr)
                self.stack.push(cond)
                return end_false
            next_jump_addr = x.jump()
            if addr[2] == jump_addr:
                true_expr = PyConst(True)
            else:
                end_true = jump_addr[-2]
                if end_true.opcode in pop_jump_if_opcodes:
                    next_jump_addr = end_true
                else:
                    end_true = x
                d_true = SuiteDecompiler(next_addr, end_true)
                d_true.run()
                true_expr = d_true.stack.pop()
            if x.arg == 0:
                false_expr = PyConst(True)
                if addr[2] == jump_addr:# if (True if a else True):
                    d_true = SuiteDecompiler(x[1], self.end_addr)
                    d_true.run()
                    if len(d_true.suite.statements) == 0:
                        stmt = SimpleStatement('pass')
                    else:
                        stmt = d_true.suite.statements[0]
                    x = Suite()
                    x.add_statement(stmt)
                    cond = PyIfElse(cond, true_expr, false_expr)
                    self.push_popjump(truthiness, self.end_block, cond, addr)
                    cond = self.pop_popjump()
                    stmt = IfStatement(cond, x, None)
                    self.suite.add_statement(stmt)
                    self.suite.statements = self.suite.statements + d_true.suite.statements[1:]
                    return self.END_NOW
                cond = PyIfElse(cond, true_expr, false_expr)
                self.push_popjump(truthiness, self.end_block, cond, addr)
                cond = self.pop_popjump()
            else:
                x = x.jump()
                end_false= x[-1]
                if end_false.opcode in pop_jump_if_opcodes:
                    next_jump_addr = end_false
                else:
                    end_false = x
                d_false = SuiteDecompiler(jump_addr, end_false)
                d_false.run()
                false_expr = d_false.stack.pop()
                cond = PyIfElse(cond, true_expr, false_expr)
            self.stack.push(cond)
            return next_jump_addr
        
        if not addr.is_else_jump and\
                (is_loop_condition or not addr.is_continue_jump):
            #case if: pass else:
            pass_addr = None
            replace_addr = None
            if not is_loop_condition and not addr.is_continue_jump:
                x = addr[1]
                while x < self.end_block:
                    if x.is_statement:
                        break
                    if x.opcode == JUMP_FORWARD and x.addr in self.code.linemap:
                        pass_addr = x
                        replace_addr = x.jump()
                        if replace_addr == j_addr:
                            jump_addr = x
                        break
                    x = x[1]
            
            self.push_popjump(truthiness, jump_addr, cond, addr)
            
            if addr not in self.code.statement_jumps:
                return None
            
            # Dictionary comprehension
            if jump_addr.seek_forward(MAP_ADD):
                return None

            if addr.code.name=='<lambda>':
                return None
            # Generator
            #if jump_addr.seek_forward(YIELD_VALUE, jump_addr.seek_stmt(None)):
                #return None

            if jump_addr.seek_back(JUMP_IF_TRUE_OR_POP,jump_addr[-2]):
                return None
            # Generator
            if addr.code.name == '<genexpr>' and jump_addr.opcode != END_FINALLY and jump_addr[1] and jump_addr[1].opcode == JUMP_ABSOLUTE:
                return None

            end_true = jump_addr
            if self.end_addr and jump_addr > self.end_addr:
                end_true = self.end_addr
            else:
                #next_addr = addr[1]
                if not is_loop_condition:
                    x = addr.seek_stmt(jump_addr)
                    if x is None:
                        x = jump_addr
                    while next_addr and next_addr < x:
                        if next_addr.opcode in pop_jump_if_opcodes:
                            next_jump_addr = next_addr.jump()
                            if not is_loop_condition:
                                if next_jump_addr == replace_addr:
                                    next_jump_addr = pass_addr
                            if next_jump_addr > jump_addr or \
                                    (next_jump_addr[-1].opcode == SETUP_LOOP):
                                return None
                            if next_addr[1] == jump_addr and addr.arg != next_addr.arg:
                                return None
                            if next_jump_addr.opcode == FOR_ITER:
                                return None

                        if next_addr.opcode in (JUMP_IF_FALSE_OR_POP, JUMP_IF_TRUE_OR_POP):
                            next_jump_addr = next_addr.jump()
                            if next_jump_addr > jump_addr or (next_jump_addr == jump_addr and jump_addr[-1].opcode in else_jump_opcodes):
                                return None
                        next_addr = next_addr[1]
            
            cond = self.pop_popjump()
            d_true = SuiteDecompiler(addr[1], end_true)
            d_true.run()
            stmt = IfStatement(cond, d_true.suite, None)
            self.suite.add_statement(stmt)
            return end_true

        if addr not in self.code.statement_jumps:
            self.push_popjump(truthiness, jump_addr, cond, addr)
            return
        
        end_true = jump_addr[-1]
        if self.end_addr and jump_addr > self.end_addr:
            if jump_addr == j_addr: #fast fix conflict with genexp.
                end_true = self.end_addr

        is_assert = \
            end_true.opcode == RAISE_VARARGS and \
            next_addr.opcode == LOAD_GLOBAL and \
            next_addr.code.names[next_addr.arg].name == 'AssertionError'
        is_assert = False#By default the compiler generates nothing on assert statement
        
        if truthiness and addr.is_continue_jump and\
                isinstance(cond, PyCompare) and cond.complist[1].startswith('is'):
            c = next_addr
            x = False
            while c <= self.end_block:
                if c.is_statement:
                    break
                if c.opcode in pop_jump_if_opcodes:
                    x = True
                elif c.opcode == JUMP_ABSOLUTE:
                    if c.is_continue_jump:
                        self.push_popjump(truthiness, c, cond, addr)
                        return None
                    else:
                        break#what is it?
                c = c[1]
        
        if in_loop and not is_loop_condition and not truthiness and addr.is_else_jump and\
                next_addr < self.end_block and next_addr.addr in self.code.linemap:
            if (next_addr.opcode == JUMP_FORWARD) or (next_addr.opcode == JUMP_ABSOLUTE and next_addr.is_continue_jump):
                faraddr = self.popjump_stack_farthestaddr()
                if (faraddr is not None) and faraddr != jump_addr and faraddr > next_addr:
                    x = addr[2]
                    if (not addr.is_continue_jump) or (x.opcode == JUMP_ABSOLUTE and x.is_continue_jump):
                        #if ... or ...: pass else:...
                        stmt = SimpleStatement('pass')
                        d_true = Suite()
                        d_true.add_statement(stmt)
                        if x == faraddr:
                            x = SimpleStatement('pass')
                            d_false = Suite()
                            d_false.add_statement(x)
                        else:
                            d_false = SuiteDecompiler(x, faraddr)
                            d_false.run()
                            d_false = d_false.suite
                        cond = self.popjump_stack_ifpassgetcond(truthiness, jump_addr, cond, addr, next_addr, faraddr)
                        stmt = IfStatement(cond, d_true, d_false)
                        self.suite.add_statement(stmt)
                        if faraddr == self.end_block:
                            return self.END_NOW
                        return faraddr
        
        self.push_popjump(truthiness, jump_addr, cond, addr)
        cond = self.pop_popjump()
        
        if addr in self.code.end_chained_jumps:
            if next_addr.opcode in (JUMP_ABSOLUTE, JUMP_FORWARD) and next_addr[1].opcode == POP_TOP:
                if truthiness:# if not(a<b<c):
                    next_addr = next_addr[2]
                else:# if (a<b<c):
                    next_addr = next_addr[3]
        
        if in_loop and addr.is_continue_jump:
            d_true = SuiteDecompiler(next_addr, self.end_addr)
            d_true.scan_for_else = True
            end_true = d_true.run()
            if end_true and end_true < self.end_block:
                if end_true.opcode == JUMP_FORWARD:
                    d_false = SuiteDecompiler(end_true[1], end_true.jump())
                    d_false.run()
                    self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                    return end_true.jump()
                elif end_true.opcode in (JUMP_ABSOLUTE, RETURN_VALUE):
                    d_false = SuiteDecompiler(end_true[1], self.end_addr)
                    d_false.run()
                    end_false = None
                    x = len(d_false.suite.statements)
                    if x < 2:
                        if x == 1:
                            if self.end_block.addr in self.code.linemap and self.end_addr.index == wcontext[2]:
                                d_false.suite.statements[0].val = "pass"
                                self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                            elif end_true.opcode == JUMP_ABSOLUTE and end_true.addr not in self.code.linemap:
                                self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                            else:
                                self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                                self.suite.add_statement(d_false.suite.statements[0])#continue
                        else:
                            self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                        return self.END_NOW
                    c = -1
                    i = 0
                    while i < x:
                        stmt = d_false.suite.statements[i]
                        if isinstance(stmt, SimpleStatement):
                            if stmt.val == "continue":
                                c = i
                            elif stmt.val.startswith("return"):
                                end_false = i + 1
                                break
                        i = i + 1
                    #assert c >= 0
                    if end_false is None:
                        if c <= 0:
                            if self.end_block.addr in self.code.linemap and self.end_block.opcode == JUMP_ABSOLUTE:
                                self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                                self.suite.statements = self.suite.statements + d_false.suite.statements
                            else:
                                self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                            return self.END_NOW
                        else:
                            end_false = c
                    elif end_false == x and self.end_block.opcode == RETURN_VALUE:
                        if c <= 0:
                            self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                            self.suite.statements = self.suite.statements + d_false.suite.statements
                            return self.END_NOW
                        else:
                            end_false = c
                    self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                    self.suite.statements = self.suite.statements + d_false.suite.statements[end_false:]
                    d_false.suite.statements = d_false.suite.statements[:end_false]
                    return self.END_NOW
                else:
                    raise Exception('scan_for_else: end_true<end_block.')
            else:
                end_true = None
                x = len(d_true.suite.statements)
                #assert x > 0
                if x < 2:
                    if next_addr == self.end_block and next_addr.is_continue_jump:
                        d_true.suite.statements[0].val = "pass"
                    self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                    return self.END_NOW
                i = 1
                while i < x:
                    stmt = d_true.suite.statements[i]
                    if isinstance(stmt, SimpleStatement) and stmt.val == "continue":
                        end_true = i
                        break
                    i = i + 1
                self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                if end_true is not None:
                    #continue after if statement
                    self.suite.statements = self.suite.statements + d_true.suite.statements[end_true:]
                    d_true.suite.statements = d_true.suite.statements[:end_true]
                return self.END_NOW
        
        if end_true.opcode == RETURN_VALUE and not addr.is_continue_jump and j_addr <= self.end_block:
            d_true = SuiteDecompiler(next_addr, end_true[1])
            d_true.run()
            stmt = d_true.suite.statements[-1]
            if not (isinstance(stmt, SimpleStatement) and (stmt.val.startswith("return") or stmt.val == "yield")):
                self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                return j_addr
            d_false = SuiteDecompiler(j_addr, self.end_addr)
            d_false.run()
            x = len(d_false.suite.statements)
            for i in range(x):
                stmt = d_false.suite.statements[i]
                if isinstance(stmt, SimpleStatement) and stmt.val.startswith("return"):
                    if i < x - 1:
                        stmt = d_false.suite.statements[i + 1]
                        if isinstance(stmt, SimpleStatement) and stmt.val == "yield":
                            i = i + 1
                    if i < x - 1:
                        end_false = i + 1
                        self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                        self.suite.statements = self.suite.statements + d_false.suite.statements[end_false:]
                        d_false.suite.statements = d_false.suite.statements[:end_false]
                        return self.END_NOW
                    c = self.end_block[1]
                    end_false = False
                    if self.end_block.opcode != RETURN_VALUE or\
                            (c and (c.opcode == JUMP_FORWARD or (c.opcode == JUMP_ABSOLUTE and c.addr not in self.code.linemap))):
                        end_false = True
                    elif not self.end_addr:
                        c = self.end_block[-1]
                        if c.opcode == LOAD_CONST and\
                                c[-1].opcode not in (JUMP_IF_TRUE_OR_POP, JUMP_IF_FALSE_OR_POP):
                            x = self.code.consts[c.arg]
                            if x.val is None:
                                if self.code.flags.generator:
                                    end_false = self.code[0].seek_forward(RETURN_VALUE, c) is not None
                                else:
                                    end_false = c.addr not in self.code.linemap
                                if end_false:
                                    for x in self.code.ternaryop_jumps:
                                        if x.jump() == c:
                                            end_false = False
                                            break
                    if end_false:
                        self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                        return self.END_NOW
                    break
            self.suite.add_statement(IfStatement(cond, d_true.suite, None))
            self.suite.statements = self.suite.statements + d_false.suite.statements
            return self.END_NOW

        if is_assert:
            # cond = cond.operand if isinstance(cond, PyNot) else PyNot(cond)
            d_true = SuiteDecompiler(next_addr, end_true)
            d_true.run()
            assert_pop = d_true.stack.pop()
            assert_args = assert_pop.args if isinstance(assert_pop, PyCallFunction) else []
            assert_arg_str = ', '.join(map(str,[cond, *assert_args]))
            self.suite.add_statement(SimpleStatement(f'assert {assert_arg_str}'))
            return end_true[1]

        # - If the true clause ends in RAISE_VARARGS, then it's an
        # assert statement. For now I just write it as a raise within
        # an if (see below)
        if end_true.opcode in (RAISE_VARARGS, POP_TOP):
            d_true = SuiteDecompiler(next_addr, end_true[1])
            d_true.run()
            self.suite.add_statement(IfStatement(cond, d_true.suite, Suite()))
            return jump_addr
        
        
        
        if j_addr > self.end_block:
            d_true = SuiteDecompiler(next_addr, self.end_addr)
            d_true.run()
            
            end_true = None
            x = len(d_true.suite.statements)
            for i in range(x):
                stmt = d_true.suite.statements[i]
                if isinstance(stmt, SimpleStatement) and stmt.val.startswith("return"):
                    if i < x - 2 or self.end_block.is_continue_jump:
                        end_true = i
                        break
                    if i == x - 2:
                        stmt = d_true.suite.statements[i + 1]
                        if not (isinstance(stmt, SimpleStatement) and stmt.val == "yield"):
                            end_true = i + 1
                    break
            if end_true is None:
                if self.end_block.opcode == JUMP_FORWARD and self.end_block.arg == 0: #self.end_block[1] == j_addr
                    d_false = Suite()
                    d_false.add_statement(SimpleStatement('pass'))
                    self.suite.add_statement(IfStatement(cond, d_true.suite, d_false))
                else:
                    self.suite.add_statement(IfStatement(cond, d_true.suite, None))
            else:
                end_true = end_true + 1
                self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                self.suite.statements = self.suite.statements + d_true.suite.statements[end_true:]
                d_true.suite.statements = d_true.suite.statements[:end_true]
            return self.END_NOW
        
        if in_loop and not is_loop_condition and end_true.is_continue_jump:
            d_true = SuiteDecompiler(next_addr, j_addr)
            if end_true.addr in self.code.linemap:
                if (next_addr.index + 1 == j_addr.index) and (self.end_addr.index == wcontext[2]):
                    d_false = SuiteDecompiler(j_addr, self.end_addr)
                    d_false.run()
                    x = len(d_false.suite.statements)
                    if x > 1:
                        for i in range(x):
                            stmt = d_false.suite.statements[i]
                            if isinstance(stmt, SimpleStatement) and stmt.val.startswith("return"):
                                if x == 1 or i < x-1 or self.end_block.opcode == JUMP_ABSOLUTE:
                                    i = i + 1
                                    self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                                    self.suite.statements = self.suite.statements + d_false.suite.statements[i:]
                                    d_false.suite.statements = d_false.suite.statements[:i]
                                    return self.END_NOW
                    stmt = d_false.suite.statements[-1]
                    if isinstance(stmt, SimpleStatement) and stmt.val.startswith("return") and self.end_block.opcode == JUMP_ABSOLUTE:
                        d_true.suite.add_statement(SimpleStatement('pass'))
                        self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                    else:
                        d_true.suite.add_statement(SimpleStatement('continue'))
                        self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                        self.suite.statements = self.suite.statements + d_false.suite.statements
                    return self.END_NOW
                else:
                    d_true.run()
                    self.suite.add_statement(IfStatement(cond, d_true.suite, None))
                    return j_addr
            else:
                d_true.run()
                d_false = SuiteDecompiler(j_addr, self.end_addr)
                d_false.run()
                self.suite.add_statement(IfStatement(cond, d_true.suite, d_false.suite))
                
                x = len(d_false.suite.statements)
                for i in range(x):
                    stmt = d_false.suite.statements[i]
                    if isinstance(stmt, SimpleStatement) and stmt.val.startswith("return"):
                        if x == 1 or i < x-1 or self.end_block.opcode == JUMP_ABSOLUTE:
                            i = i + 1
                            self.suite.statements = self.suite.statements + d_false.suite.statements[i:]
                            d_false.suite.statements = d_false.suite.statements[:i]
                            return self.END_NOW
                i = 0
                while i < x:
                    stmt = d_false.suite.statements[i]
                    if isinstance(stmt, SimpleStatement) and stmt.val == "continue":
                        self.suite.statements = self.suite.statements + d_false.suite.statements[i:]
                        d_false.suite.statements = d_false.suite.statements[:i]
                        break
                    i = i + 1
                return self.END_NOW
        
        d_true = SuiteDecompiler(next_addr, end_true)
        
        # It's an if-else (expression or statement)
        if end_true.opcode == JUMP_FORWARD:
            end_false = end_true.jump()
        elif end_true.opcode == JUMP_ABSOLUTE:
            if end_true.is_continue_jump:
                d_true.end_addr = end_true[1]
                end_false = self.end_addr
            else:
                end_false = end_true.jump()
                if end_false > self.end_block:
                    end_false = self.end_addr
                elif end_false.opcode == RETURN_VALUE:
                    end_false = end_false[1]
                
        elif end_true.opcode == RETURN_VALUE:
            # find the next RETURN_VALUE
            end_false = jump_addr
            while end_false.opcode != RETURN_VALUE:
                end_false = end_false[1]
            end_false = end_false[1]
        elif end_true.opcode == BREAK_LOOP:
            # likely in a loop in a try/except
            end_false = jump_addr
        else:
            end_false = jump_addr
            # # normal statement
            # raise Exception("#ERROR: Unexpected statement: {} | {}\n".format(end_true, jump_addr, jump_addr[-1]))
            # # raise Unknown
            # jump_addr = end_true[-2]
            # stmt = IfStatement(cond, d_true.suite, None)
            # self.suite.add_statement(stmt)
            # return jump_addr or self.END_NOW
        if jump_addr == addr[2] and next_addr.opcode == JUMP_FORWARD:
            d_true.suite.add_statement(SimpleStatement('pass'))
        else:
            d_true.run()
        d_false = SuiteDecompiler(jump_addr, end_false)
        if end_true.opcode == JUMP_FORWARD and end_true.arg == 0:
            d_false.suite.add_statement(SimpleStatement('pass'))
        else:
            d_false.run()
        if d_true.stack and d_false.stack:
            assert len(d_true.stack) == len(d_false.stack) == 1
            # self.write("#ERROR: Unbalanced stacks {} != {}".format(len(d_true.stack),len(d_false.stack)))
            assert not (d_true.suite or d_false.suite)
            # this happens in specific if else conditions with assigments
            true_expr = d_true.stack.pop()
            false_expr = d_false.stack.pop()
            self.stack.push(PyIfElse(cond, true_expr, false_expr))
        else:
            stmt = IfStatement(cond, d_true.suite, d_false.suite)
            self.suite.add_statement(stmt)
        return end_false or self.END_NOW

    def POP_JUMP_IF_FALSE(self, addr, target):
        return self.POP_JUMP_IF(addr, target, truthiness=False)

    def POP_JUMP_IF_TRUE(self, addr, target):
        return self.POP_JUMP_IF(addr, target, truthiness=True)

    def JUMP_ABSOLUTE(self, addr, target):
        # return addr.jump()

        if addr.is_continue_jump:
            if addr.addr in self.code.linemap:
                self.suite.add_statement(SimpleStatement('continue'))

    #
    # For loops
    #

    def GET_ITER(self, addr):
        pass

    def FOR_ITER(self, addr: Address, delta):
        if addr[-1] and addr[-1].opcode == RETURN_VALUE:
            # Dead code
            return self.END_NOW
        iterable = self.stack.pop()
        jump_addr = addr.jump()
        end_body = jump_addr
        if end_body.opcode != POP_BLOCK:
            end_body = end_body[-1]
        while True:
            d_body = SuiteDecompiler(addr[1], end_body)
            for_stmt = ForStatement(iterable)
            d_body.stack.push(for_stmt)
            d_body.run()
            if d_body.verify_loop_laststmt(d_body.suite, addr[1], end_body):
                break
        for_stmt.body = d_body.suite
        loop = addr.seek_back(SETUP_LOOP)
        end_addr = jump_addr
        if loop and jump_addr.opcode == POP_BLOCK:
            end_of_loop = loop.jump()
            if end_of_loop > self.end_block:
                end_of_loop = self.end_addr
            if end_of_loop.opcode != POP_BLOCK:
                else_start = jump_addr[1]
                if else_start < end_of_loop:
                    d_else = SuiteDecompiler(else_start, end_of_loop)
                    d_else.run()
                    for_stmt.else_body = d_else.suite
                    end_addr = end_of_loop
        self.suite.add_statement(for_stmt)
        return end_addr

    # Function creation

    def MAKE_FUNCTION_OLD(self, addr, argc, is_closure=False):
        testType = self.stack.pop().val
        if isinstance(testType, str):
            code = Code(self.stack.pop().val, self.code)
        else:
            code = Code(testType, self.code)
        closure = self.stack.pop() if is_closure else None
        # parameter annotation objects
        paramobjs = {}
        paramcount = (argc >> 16) & 0x7FFF
        if paramcount:
            paramobjs = dict(zip(self.stack.pop().val, self.stack.pop(paramcount - 1)))
        # default argument objects in positional order 
        defaults = self.stack.pop(argc & 0xFF)
        # pairs of name and default argument, with the name just below the object on the stack, for keyword-only parameters 
        kwdefaults = {}
        for i in range((argc >> 8) & 0xFF):
            k, v = self.stack.pop(2)
            if hasattr(k, 'name'):
                kwdefaults[k.name] = v
            elif hasattr(k, 'val'):
                kwdefaults[k.val] = v
            else:
                kwdefaults[str(k)] = v
        func_maker = code_map.get(code.name, DefStatement)
        self.stack.push(func_maker(code, defaults, kwdefaults, closure, paramobjs))

    def MAKE_FUNCTION_NEW(self, addr, argc, is_closure=False):
        testType = self.stack.pop().val
        if isinstance(testType, str):
            code = Code(self.stack.pop().val, self.code)
        else:
            code = Code(testType, self.code)
        closure = self.stack.pop() if is_closure else None
        annotations = None
        kwdefaults = {}
        defaults = {}
        paramobjs = {}
        if argc & 8:
            annotations = list(self.stack.pop())
        if argc & 4:
            #paramobjs = self.stack.pop()
            #if isinstance(paramobjs, PyDict):
                #paramobjs = {str(k[0].val).replace('\'', ''): str(k[1]) for k in paramobjs.items}
            p = self.stack.pop()
            if isinstance(p, PyDict):
                for nm,an in p.items:
                    nm = str(nm.val).replace('\'', '')
                    if isinstance(an, PyConst) and isinstance(an.val, str):
                        paramobjs[nm] = an.val
                    else:
                        paramobjs[nm] = str(an)
            else:
                paramobjs = p
        if argc & 2:
            kwdefaults = self.stack.pop()
            if isinstance(kwdefaults, PyDict):
                kwdefaults = {str(k[0].val): str(k[1] if isinstance(k[1], PyExpr) else PyConst(k[1])) for k in
                              kwdefaults.items}
            if not kwdefaults:
                kwdefaults = {}
        if argc & 1:
            defaults = list(map(lambda x: str(x if isinstance(x, PyExpr) else PyConst(x)), self.stack.pop()))
        func_maker = code_map.get(code.name, DefStatement)
        self.stack.push(func_maker(code, defaults, kwdefaults, closure, paramobjs, annotations))

    def MAKE_FUNCTION(self, addr, argc, is_closure=False):
        if sys.version_info < (3, 6):
            self.MAKE_FUNCTION_OLD(addr, argc, is_closure)
        else:
            self.MAKE_FUNCTION_NEW(addr, argc, is_closure)

    def LOAD_CLOSURE(self, addr, i):
        # Push the varname.  It doesn't matter as it is not used for now.
        self.stack.push(self.code.derefnames[i])

    def MAKE_CLOSURE(self, addr, argc):
        self.MAKE_FUNCTION(addr, argc, is_closure=True)

    #
    # Raising exceptions
    #

    def RAISE_VARARGS(self, addr, argc):
        # TODO: find out when argc is 2 or 3
        # Answer: In Python 3, only 0, 1, or 2 argument (see PEP 3109)
        if argc == 0:
            self.write("raise")
        elif argc == 1:
            exception = self.stack.pop()
            self.write("raise {}", exception)
        elif argc == 2:
            from_exc, exc = self.stack.pop(), self.stack.pop()
            self.write("raise {} from {}".format(exc, from_exc))
        else:
            raise Unknown

    def EXTENDED_ARG(self, addr, ext):
        # self.write("# ERROR: {} : {}".format(addr, ext) )
        pass

    def WITH_CLEANUP(self, addr, *args, **kwargs):
        # self.write("# ERROR: {} : {}".format(addr, args))
        pass

    def WITH_CLEANUP_START(self, addr, *args, **kwargs):
        pass

    def WITH_CLEANUP_FINISH(self, addr, *args, **kwargs):
        jaddr = addr.jump()
        return jaddr

    # Formatted string literals
    def FORMAT_VALUE(self, addr, flags):
        formatter = ''
        if (flags & 0x03) == 0x01:
            formatter = '!s'
        elif (flags & 0x03) == 0x02:
            formatter = '!r'
        elif (flags & 0x03) == 0x03:
            formatter = '!a'
        if (flags & 0x04) == 0x04:
            formatter = formatter + ':' + self.stack.pop().val
        val = self.stack.pop()
        f = PyFormatValue(val)
        f.formatter = formatter
        self.stack.push(f)

    def BUILD_STRING(self, addr, c):
        params = self.stack.pop(c)
        self.stack.push(PyFormatString(params))

    # Coroutines
    def GET_AWAITABLE(self, addr: Address):
        func: AwaitableMixin = self.stack.pop()
        func.is_awaited = True
        self.stack.push(func)
        yield_op = addr.seek_forward(YIELD_FROM)
        return yield_op[1]

    def BEFORE_ASYNC_WITH(self, addr: Address):
        with_addr = addr.seek_forward(SETUP_ASYNC_WITH)
        end_with = with_addr.jump()
        with_stmt = WithStatement(self.stack.pop())
        with_stmt.is_async = True
        d_with = SuiteDecompiler(addr[1], end_with)
        d_with.stack.push(with_stmt)
        d_with.run()
        with_stmt.suite = d_with.suite
        self.suite.add_statement(with_stmt)
        if sys.version_info <= (3, 4):
            assert end_with.opcode == WITH_CLEANUP
            assert end_with[1].opcode == END_FINALLY
            return end_with[2]
        else:
            assert end_with.opcode == WITH_CLEANUP_START
            assert end_with[1].opcode == GET_AWAITABLE
            assert end_with[4].opcode == WITH_CLEANUP_FINISH
            return end_with[5]

    def SETUP_ASYNC_WITH(self, addr: Address, arg):
        pass

    def GET_AITER(self, addr: Address):
        return addr[2]

    def GET_ANEXT(self, addr: Address):
        iterable = self.stack.pop()
        for_stmt = ForStatement(iterable)
        for_stmt.is_async = True
        jump_addr = addr[-1].jump()
        d_body = SuiteDecompiler(addr[3], jump_addr[-1])
        d_body.stack.push(for_stmt)
        d_body.run()
        jump_addr = jump_addr[-1].jump()
        new_start = jump_addr
        new_end = jump_addr[-2].jump()[-1]
        d_body.start_addr = new_start

        d_body.end_addr = new_end

        d_body.run()

        for_stmt.body = d_body.suite
        self.suite.add_statement(for_stmt)
        new_end = new_end.seek_forward(POP_BLOCK)
        return new_end


def make_dynamic_instr(cls):
    def method(self, addr):
        cls.instr(self.stack)

    return method


# Create unary operators types and opcode handlers
for op, name, ptn, prec in unary_ops:
    name = 'Py' + name
    tp = type(name, (PyUnaryOp,), dict(pattern=ptn, precedence=prec))
    globals()[name] = tp
    setattr(SuiteDecompiler, op, make_dynamic_instr(tp))

# Create binary operators types and opcode handlers
for op, name, ptn, prec, inplace_ptn in binary_ops:
    # Create the binary operator
    tp_name = 'Py' + name
    tp = globals().get(tp_name, None)
    if tp is None:
        tp = type(tp_name, (PyBinaryOp,), dict(pattern=ptn, precedence=prec))
        globals()[tp_name] = tp

    setattr(SuiteDecompiler, 'BINARY_' + op, make_dynamic_instr(tp))
    # Create the in-place operation
    if inplace_ptn is not None:
        inplace_op = "INPLACE_" + op
        tp_name = 'InPlace' + name
        tp = type(tp_name, (InPlaceOp,), dict(pattern=inplace_ptn))
        globals()[tp_name] = tp
        setattr(SuiteDecompiler, inplace_op, make_dynamic_instr(tp))

if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        print('USAGE: {} <filename.pyc>'.format(sys.argv[0]))
    else:
        print(decompile(sys.argv[1])) 
