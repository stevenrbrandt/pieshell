#!/usr/bin/env python3 
# Purpose of Piebash
# (1) Security for Science Gateways
# (2) Supercharge Jupyter: Allow in-process calling of bash from Python, save ENVIRON variables, etc.
# (3) Call python functions from bash or bash functions from python
from pwd import getpwnam, getpwuid
from Piraha import parse_peg_src, Matcher, Group, set_trace
from subprocess import Popen, PIPE, STDOUT
from pipe_threads import PipeThread, get_lastpid, get_running
import os
import sys
import re
from traceback import print_exc
from here import here
from shutil import which
from datetime import datetime

class ExitShell(Exception):
    def __init__(self, rc):
        self.rc = rc

import io
home = os.environ["HOME"]

my_shell = os.path.realpath(sys.argv[0])

Never = object()

class ContinueException(Exception):
    def __init__(self,message):
        super().__init__(message)
        self.message = message

class TFN:
    """
    This class has values of True, False, and Never.
    """

    def __init__(self, b):
        if b in [True, False]:
            self.b = b
        elif b == "True":
            self.b = True
        elif b == "False":
            self.b = False
        else:
            self.b = Never

    def toggle(self):
        """
        Toggling turns True to False, False to True,
        and leaves Never alone.
        """
        if self.b == True:
            self.b = False
        elif self.b == False:
            self.b = True
        elif self.b == Never:
            pass
        else:
            raise Exception("bad state")

    def __bool__(self):
        if self.b in [True, False]:
            return self.b
        else:
            return False

def unesc(s):
    """
    Remove one level of escapes (backslashes) from a string.
    """
    s2 = ""
    i = 0
    while i < len(s):
        if s[i] == '\\':
            s2 += s[i+1]
            i += 2
        else:
            s2 += s[i]
            i += 1
    return s2

verbose = False

from colored import colored

grammar = r"""
skipper=\b([ \t]|\\\n|\#.*)*
s=\b([ \t\n]|\\\n|\#.*)*
raw_word=(\\.|[^\\"'\t \n\$\#&\|;{}`()<>?*,])+
dchar=[^"$\\]
dlit=\\.
dquote="({dlit}|{dchar}|{var}|{math}|{subproc}|{bquote})*"
bquote=`(\\.|[^`$]|{var}|{math}|{subproc})*`
squote='(\\.|[^'])*'
unset=:-
unset_raw=-
rm_front=\#\#?
rm_back=%%?
wchar=[@?$!]
w=[a-zA-Z0-9_]+
var=\$({wchar}|{w}|\{(({w}|{wchar})({unset}{words2}|{unset_raw}{raw_word}|{rm_front}{word2}|{rm_back}{word2}|))\})
func=function {ident} \( \) \{( {cmd})* \}({redir}|[ \t])*\n

worditem=({glob}|{redir}|{ml}|{var}|{math}|{subproc}|{raw_word}|{squote}|{dquote}|{bquote})
worditemex=({glob}|{redir}|{ml}|{var}|{math}|{subproc}|{raw_word}|{squote}|{dquote}|{bquote}|{expand})
word={expand}{-worditemex}+|{-worditem}{-worditemex}*

word2=({glob}|{redir}|{ml}|{var}|{math}|{subproc}|{raw_word}|{squote}|{dquote}|{bquote})
words2={word2}+

ending=(&&?|\|[\|&]?|;(?!;)|\n|$)
esac=esac
casepattern=[^ \t\)]*\)
case=case {word} in({-s}{casepattern}|)
case2=;;{-s}({esac}|{casepattern}|)

fd_from=[0-9]+
fd_to=&[0-9]+
ltgt=(<|>>|>)
redir=({fd_from}|){ltgt}( {fd_to}| {word})
ident=[a-zA-Z0-9][a-zA-Z0-9_]*
ml=<< {ident}
mathchar=(\)[^)]|[^)])
math=\$\(\(({var}|{mathchar})*\)\)
subproc=\$\(( {cmd})* \)
cmd={subshell}|(( {word})+( {ending}|)|{ending})
glob=\?|\*|\[.-.\]
expand=[\{,\}]
subshell=\(( {cmd})* \)
whole_cmd=^( ({func}|{case}|{case2}|{cmd}))* $
"""
pp,_ = parse_peg_src(grammar)

class For:
    """
    A data structure used to keep track of
    the information needed to implement
    for loops.
    """
    def __init__(self,variable,values):
        self.variable = variable
        self.values = values
        self.index = 0
        self.docmd = -1
        self.donecmd = -1
    def __repr__(self):
        return f"For({self.variable},{self.values},{self.docmd},{self.donecmd})"

class Space:
    """
    This class represents a literal space
    """
    def __repr__(self):
        return " "

def spaceout(a):
    """
    Put a space between each member of a list
    """
    b = []
    for i in range(len(a)):
        if i > 0:
            b += [Space()]
        b += [a[i]]
    return b

def deglob(a):
    """
    Process a file glob
    """
    assert type(a) == list
    has_glob = False
    for k in a:
        if isinstance(k, Group):
            has_glob = True
    if not has_glob:
        return a

    s = []
    raw = ''
    for k in a:
        if isinstance(k,Group) and k.is_("glob"):
            ks = k.substring()
            s += [("g"+ks,k)]
            raw += ks
        elif isinstance(k,Group) and k.is_("expand"):
            here("remove this")
        elif type(k) == str:
            for c in k:
                s += [(c,)]
            raw += k
        else:
            assert False
    files = fmatch(None, s, i1=0, i2=0)
    if len(files) == 0:
        return [raw]
    else:
        return spaceout(files)

class Expando:
    """
    Bookkeeping class used by expandCurly.
    """
    def __init__(self):
        self.a = [[]]
        self.parent = None

    def start_new_list(self):
        e = Expando()
        e.parent = self
        self.a[-1] += [e]
        return e

    def start_new_alternate(self):
        self.a += [[]]

    def end_list(self):
        return self.parent

    def add_item(self, item):
        self.a[-1] += [item]

    def __repr__(self):
        return "Expando("+str(self.a)+")"

    def build_strs(self):
        streams = [a for a in self.a]
        final_streams = []
        show = False
        while len(streams) > 0:
            new_streams = []
            for stream in streams:
                found = False
                for i in range(len(stream)):
                    item = stream[i]
                    if isinstance(item,Expando):
                        found = True
                        show = True
                        for a in item.a:
                            new_stream = stream[:i]+a+stream[i+1:]
                            new_streams += [new_stream]
                        break
                if not found:
                    final_streams += [stream]
            streams = new_streams
        return final_streams
            
def expandCurly(a,ex=None,i=0,sub=0):
    """
    The expandCurly method expands out curly braces on the command line,
    e.g. "echo {a,b}{c,d}" should produce "ac ad bc bd".
    """
    if ex is None:
        ex = Expando()
    sub = 0
    for i in range(len(a)):
        if isinstance(a[i], Group) and a[i].is_("expand"):
            if a[i].substring() == "{":
                ex = ex.start_new_list()
            elif a[i].substring() == '}':
                ex = ex.end_list()
            elif a[i].substring() == ',':
                ex.start_new_alternate()
            else:
                ex.add_item(a[i])
        else:
            ex.add_item(a[i])
    return ex

def fmatch(fn,pat,i1=0,i2=0):
    """
    Used by deglob() in processing globs in filenames.
    """
    while True:
        if fn is None:
            result = []
            if pat[0] == ('/',):
                for d in os.listdir(fn):
                    result += fmatch('/'+d, pat, 1, 1)
            else:
                for d in os.listdir('.'):
                    result += fmatch(d, pat, 0, 0)
            return result
        elif i2 == len(pat) and i1 == len(fn):
            return [fn]
        elif i1 == len(fn) and pat[i2] == ('/',):
            dd = []
            for k in os.listdir(fn):
                ff = os.path.join(fn,k)
                if i1 <= len(ff):
                    dd += fmatch(os.path.join(fn,k), pat, i1, i2)
            return dd
        elif i2 >= len(pat):
            return []
        elif pat[i2][0] == 'g?':
            # g? is a glob ? pattern
            i1 += 1
            i2 += 1
        elif pat[i2][0] == 'g*':
            # g* is a glob * pattern
            if i2+1 <= len(pat):
                result = fmatch(fn, pat, i1, i2+1)
            else:
                restult = []
            if i1+1 <= len(fn):
                if len(result) == 0:
                    result = fmatch(fn, pat, i1+1, i2+1)
                if len(result) == 0:
                    result = fmatch(fn, pat, i1+1, i2)
            return result
        elif i1 < len(fn) and i2 < len(pat) and (fn[i1],) == pat[i2]:
            i1 += 1
            i2 += 1
        else:
            return []

def cat(a, b):
    assert type(a) == list
    if type(b) == list:
        a += b
    elif type(b) == str:
        if len(a) == 0:
            a += [""]
        a[-1] += b
    else:
        assert False

def expandtilde(s):
    if type(s) == str:
        if s.startswith("~/"):
            return home + s[1:]
        if len(s)>0 and s[0] == '~':
            g = re.match(r'^~(\w+)/(.*)', s)
            if g:
                try:
                    pw = getpwnam(g.group(1))
                    if pw is not None:
                        return pw.pw_dir+"/"+g.group(2)
                except:
                    pass
        return s
    elif type(s) == list and len(s)>0:
        return [expandtilde(s[0])] + s[1:]
    else:
        return s

class shell:
    
    def __init__(self,stdout = sys.stdout, stderr = sys.stderr, stdin = sys.stdin):
        self.txt = ""
        self.vars = {"?":"0", "PWD":os.path.realpath(os.getcwd()),"*":" ".join(sys.argv[2:]), "SHELL":os.path.realpath(sys.argv[0]), "PYSHELL":"1"}
        pwdata = getpwuid(os.getuid())
        self.vars["USER"] = pwdata.pw_name
        self.vars["LOGNAME"] = pwdata.pw_name
        self.vars["HOME"] = pwdata.pw_dir

        # Command line args
        args = sys.argv[2:]
        self.vars["@"] = " ".join(args)
        for vnum in range(len(args)):
            self.vars[str(vnum+1)] = args[vnum]

        self.exports = set()
        for var in os.environ:
            if var not in self.vars:
                self.vars[var] = os.environ[var]
            self.exports.add(var)
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.lines = []
        self.cmds = []
        self.stack = []
        self.for_loops = []
        self.case_stack = []
        self.funcs = {}
        self.output = ""
        self.error = ""
        self.save_in = []
        self.save_out = []
        self.last_ending = None
        self.curr_ending = None
        self.last_pipe = None
        self.recursion = 0
        self.max_recursion_depth = 20
        self.log_fd = open(os.path.join(self.vars["HOME"],"pieshell-log.txt"),"a")
    
    def log_flush(self):
        self.log_fd.flush()

    def log_exc(self):
        print_exc()
        print("=" * 10,datetime.now(),"=" * 10,file=self.log_fd)
        self.log_flush()
        print_exc(file=self.log_fd)
        self.log_flush()

    def log(self,*args,**kwargs):
        print("=" * 10,datetime.now(),"=" * 10,file=self.log_fd)
        self.log_flush()
        print(*args,**kwargs,file=self.log_fd)
        self.log_flush()

    def set_var(self,vname,value):
        if self.allow_set_var(vname, value):
            self.vars[vname] = value

    def allow_cd(self, dname):
        return True

    def allow_cmd(self, args):
        return True

    def allow_read(self, fname):
        return True

    def allow_write(self, fname):
        return True

    def allow_append(self, fname):
        return True

    def allow_set_var(self,var,val):
        return True

    def allow_access_var(self,var):
        return True

    def lookup_var(self,gr):
        """
        lookup_var() converts a Piraha.Group to a list of strings.
        The output needs to be a list so that code like this runs
        correctly:
        ```
        a="1 2 3"
        for i in $a; do echo $i; done
        ```
        """
        varname = gr.has(0).substring()
        if varname == "$":
            return [str(os.getpid())]
        elif varname == "!":
            return [str(get_lastpid())]
        if not self.allow_access_var(varname):
            return ""
        if varname in self.vars:
            v = spaceout(re.split(r'\s+', self.vars[varname]))
        else:
            v = None
        if v is None and gr.has(1,"unset"):
            v = self.eval(gr.children[2])
        elif v is None and gr.has(1,"unset_raw"):
            v = self.eval(gr.children[2])
        rmb = gr.has(1,"rm_back")
        if rmb:
            back = gr.children[2].substring()
            if len(v) > 0 and v[0].endswith(back):
                v[0] = v[0][:-len(back)]
        if v is None:
            return [""]
        else:
            return v

    def evaltest(self, args, index=0):
        """
        Process calls to `test` or `if`, optimizing in some cases.
        """
        evalresult = None
        if args[index] == "if":
            index += 1
        if args[index] in ["[[","["]:
            start = args[index]
            index += 1
        else:
            start = None
        if index < len(args) and args[index] in ["-e","-w","r","-x","-f","-d"]:
            op = args[index]
            assert index+1 < len(args), f"No file following opertor '{op}'"
            fname = args[index+1]
            if op == "-x":
                evalresult = os.path.exists(fname) and os.access(fname, os.X_OK)
            elif op == "-r":
                evalresult = os.path.exists(fname) and os.access(fname, os.R_OK)
            elif op == "-w":
                evalresult = os.path.exists(fname) and os.access(fname, os.W_OK)
            elif op == "-e":
                evalresult = os.path.exists(fname)
            elif op == "-d":
                evalresult = os.path.isdir(fname)
            elif op == "-f":
                evalresult = os.path.isfile(fname)
            else:
                assert False
            index += 2
        if index+1 < len(args) and args[index+1] in ["=","!=","\\<","<",">","\\>"]:
            op = args[index+1]
            arg1 = args[index]
            arg2 = args[index+2]
            index += 3
            if op == "=":
                evalresult = arg1 == arg2
            elif op == "!=":
                evalresult = arg1 != arg2
            elif op in ["<","\\<"]:
                evalresult = int(arg1) < int(arg2)
            elif op in [">","\\>"]:
                evalresult = int(arg1) > int(arg2)
            else:
                assert False
        if index < len(args) and args[index] == "]":
            index += 1
            assert start == "[", f"Mismatched braces: '{start}' and '{args[index]}'"
        if index < len(args) and args[index] == "]]":
            index += 1
            assert start == "[[", f"Mismatched braces: '{start}' and '{args[index]}'"
        if start is not None:
            pass #here(args)
        if evalresult is None and args[0] == "if":
            self.evalargs(args[1:], None, False, None, index, None)
            evalresult = self.vars["?"] == "0"
        return evalresult

    def do_case(self, gr):
        word = self.case_stack[-1][0]
        rpat = re.sub(r'\*','.*',gr.substring()[:-1])+'$'
        self.case_stack[-1][1] = re.match(rpat,word)

    def mkargs(self, k):
        """
        k: An input of type Piraha.Group.
        return value: a list of strings
        """
        args = []
        # Calling eval will cause $(...) etc. to be replaced.
        ek = self.eval(k)
        if k.has(0,"dquote") or k.has(0,"squote"):
            pass
        else:
            # expand ~/ and ~username/. This should
            # not happen to quoted values.
            ek = expandtilde(ek)

        # Now the tricky part. Evaluate {a,b,c} elements of the shell.
        # This can result in multiple arguments being generated.
        exk = expandCurly(ek).build_strs()
        for nek in exk:
            # Evaluate globs
            nek = deglob(nek)
            if type(nek) == str:
                args += [nek]
            elif type(nek) == list:
                args += [""]
                for kk in nek:
                    if isinstance(kk,Space):
                        args += [""]
                    else:
                        args[-1] += kk
            else:
                assert False

        return args

    def eval(self, gr, index=-1,xending=None):
        assert type(gr) != list
        r = self.eval_(gr,index,xending)
        if r is None:
            r = []
        assert type(r) in [list, str], gr.dump()+" "+repr(type(r))+" r="+repr(r)
        return r

    def eval_(self, gr, index=-1, xending=None):
        assert type(gr) != list
        if index == -1 and not gr.is_("whole_cmd"):
            index = len(self.cmds)
            self.cmds += [gr]
        if gr.is_("whole_cmd"):
            # here("wc:",gr.dump())
            pipes = None
            result = []
            ending = None
            my_ending = None
            for c in gr.children:
                if c.has(0,"ending"):
                    continue
                result = self.eval(c,xending=my_ending)
            return result
        elif gr.is_("cmd"):
            #here("cmd:",gr.dump())
            args = []
            skip = False

            if self.last_ending == "&&" and self.vars["?"] != "0":
                skip = True
            if self.last_ending == "||" and self.vars["?"] == "0":
                skip = True
            if gr.has(-1,"ending"):
                self.curr_ending = gr.group(-1).substring()
            if self.curr_ending == "|":
                self.curr_pipe = os.pipe()
            else:
                self.curr_pipe = None
            if self.curr_pipe is not None:
                self.save_out += [self.stdout]
                self.stdout = self.curr_pipe[1]
            if self.last_pipe is not None:
                self.save_in += [self.stdin]
                self.stdin = self.last_pipe[0]

            redir = None
            for k in gr.children:
                if k.has(0,"redir"):
                    redir = k.children[0]
                if not k.is_("ending") and not k.has(0,"redir"):
                     args += self.mkargs(k)
    
            if args == ['']:
                return args
            return self.evalargs(args, redir, skip, xending, index, gr)

        elif gr.is_("glob"):
            return [gr]
        elif gr.is_("expand"):
            return [gr]
        elif gr.is_("word") or gr.is_("word2") or gr.is_("words2"):
            s = []
            for c in gr.children:
                cat(s, self.eval(c))
            if gr.has(-1,"eword"):
                here("eword found:",gr.dump())
            return s 
        elif gr.is_("raw_word"):
            return [unesc(gr.substring())]
        elif gr.is_("math"):
            mtxt = ''
            for gc in gr.children:
                if gc.is_("mathchar"):
                    mtxt += gc.substring()
                else:
                    mtxt += self.lookup_var(gc)[0]
            try:
                return str(eval(mtxt)).strip()
            except:
                return f"ERROR({mtxt})"
        elif gr.is_("func"):
            assert gr.children[0].is_("ident")
            ident = gr.children[0].substring()
            self.funcs[ident] = gr.children[1:]
        elif gr.is_("subproc"):
            out_pipe = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(1)
                os.dup(out_pipe[1])
                self.stdout = 1
                os.close(out_pipe[0])
                os.close(out_pipe[1])
                for c in gr.children:
                    self.eval(c)
                exit(int(self.vars["?"]))
            assert pid != 0
            os.close(out_pipe[1])
            result = os.read(out_pipe[0],10000).decode()
            os.close(out_pipe[0])
            rc=os.waitpid(pid,0)
            return spaceout(re.split(r'\s+',result.strip()))
        elif gr.is_("dquote"):
            s = ""
            for c in gr.children:
                r = self.eval(c)
                if type(r) == str:
                    s += r
                else:
                    assert type(r) == list, "t=%s r=%s %s" % (type(r), r, c.dump())
                    for k in r:
                        if isinstance(k,Space):
                            s += ' '
                        else:
                            s += k
            assert type(s) == str
            #here("s=",s)
            return s
        elif gr.is_("dchar"):
            return gr.substring()
        elif gr.is_("squote"):
            return gr.substring()[1:-1]
        elif gr.is_("dlit"):
            s = gr.substring()
            if s == "\\n":
                return "\n"
            elif s == "\\r":
                return "\r"
            else:
                return s[1]
        elif gr.is_("var"):
            return self.lookup_var(gr)
        elif gr.has(0,"fd_from") and gr.has(1,"fd_to"):
            fd_from = gr.children[0].substring()
            fd_to = gr.children[1].substring()
            if fd_from == "2" and fd_to == "&1":
                self.stderr = self.stdout
                return None
            elif fd_from == "1" and fd_to == "&2":
                self.stdout = self.stderr
                return None
            else:
                raise Exception(f"{fd_from} and {fd_to}")
        elif gr.is_("case"):
            if gr.has(0,"word"):
                args = self.mkargs(gr.group(0))
                assert len(args)==1
                self.case_stack += [[args[0],False]]
            assert gr.has(-1,"casepattern")
            self.do_case(gr.group(-1))
        elif gr.is_("case2"):
            if gr.has(0,"casepattern"):
                self.do_case(gr.group(0))
            elif gr.has(0,"esac"):
                self.case_stack = self.case_stack[:-1]
            else:
                assert False
        elif gr.is_("subshell"):
            out_pipe = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(1)
                self.stdout = 1
                os.dup(out_pipe[1])
                os.close(out_pipe[0])
                os.close(out_pipe[1])
                for gc in gr.children:
                    self.eval(gc)
                code = int(self.vars["?"])
                exit(code)
                raise Exception()
            os.close(out_pipe[1])
            out = os.read(out_pipe[0],10000).decode()
            os.close(out_pipe[0])
            sys.stdout.write(out)
            rc=os.waitpid(pid,0)
            self.vars["?"] = str(rc[1])
            self.log("end subshell:",self.vars["?"])
            return []
        else:
            here(gr.dump())
            raise Exception(gr.getPatternName()+": "+gr.substring())
            return [gr.substring()]

    def do_redir(self, redir, sout, serr, sin):
        out_is_error = False
        fd_from = None
        rn = 0
        if redir.has(0,"fd_from"):
            fd_from = redir.children[0].substring()
            rn += 1
        if redir.has(rn,"ltgt"):
            ltgt = redir.group(rn).substring()
            if redir.has(rn+1,"word"):
                fname = redir.group(rn+1).substring()
                if ltgt == "<":
                    if not self.allow_read(fname):
                        fname = "/dev/null"
                    sin = open(fname, "r")
                elif ltgt == ">":
                    if not self.allow_write(fname):
                        pass
                    elif fd_from is None or fd_from == "1":
                        sout = open(fname, "w")
                    elif fd_from == "2":
                        serr = open(fname, "w")
                    else:
                        assert False, redir.dump()
                elif ltgt == ">>":
                    if not self.allow_append(fname):
                        pass
                    elif fd_from is None or fd_from == "1":
                        sout = open(fname, "a")
                    elif fd_from == "2":
                        serr = open(fname, "a")
                    else:
                        assert False, redir.dump()
                else:
                    assert False, redir.dump()
            elif redir.has(rn+1,"fd_to"):
                if redir.group(rn+1).substring() == "&2":
                    assert fd_from is None or fd_from=="1"
                    if sout == -1 and serr == -1:
                        stderr = STDOUT
                        out_is_error = True
                    elif sout == -1 or serr == -1:
                        assert False
                    sout = serr
                elif redir.group(rn+1).substring() == "&1":
                    assert fd_from is None or fd_from=="2"
                    if sout == -1 and serr == -1:
                        stderr = STDOUT
                        here()
                    serr = sout
                else:
                    here(redir.dump())
                    raise Exception()
            else:
                here(redir.dump())
                raise Exception()
        else:
            here(redir.dump())
            raise Exception()
        return sout, serr, sin, out_is_error

    def evalargs(self, args, redir, skip, xending, index, gr):
        try:
            if len(args)>0:
                if args[0] == "do":
                    f = self.for_loops[-1]
                    if f.docmd == -1:
                        f.docmd = index
                    args = args[1:]
                    if len(args) == 0:
                        return

                if args[0] == 'export':
                    for a in args[1:]:
                        g = re.match(r'^(\w+)=(.*)',a)
                        if g:
                            varname = g.group(1)
                            value = g.group(2)
                            #self.vars[varname] = value
                            self.set_var(varname,value)
                            self.exports.add(varname)
                        elif a in self.vars:
                            self.exports.add(varname)
                    return

                if args[0] == "for":
                    f = For(args[1],args[3:])
                    assert args[2] == "in", "Syntax: for var in ..."
                    self.for_loops += [f]
                    if f.index < len(f.values):
                        #self.vars[f.variable] = f.values[f.index]
                        self.set_var(f.variable, f.values[f.index])
                    return

                if args[0] == "done":
                    f = self.for_loops[-1]
                    assert f.docmd != -1
                    f.donecmd = index
                    if len(f.values) > 1:
                        for ii in range(1,len(f.values)):
                            f.index = ii
                            #self.vars[f.variable] = f.values[f.index]
                            self.set_var(f.variable, f.values[f.index])
                            for cmdnum in range(f.docmd,f.donecmd):
                                self.eval(self.cmds[cmdnum], cmdnum)
                    self.for_loops = self.for_loops[:-1]
                    return

                if args[0] == "then":
                    args = args[1:]
                    if len(args) == 0:
                        return

                elif args[0] == "else":
                    args = args[1:]
                    self.stack[-1][1].toggle()
                    if len(args) == 0:
                        return

                if args[0] == "if":
                    testresult = None
                    if len(self.stack) > 0 and not self.stack[-1][1]:
                        # initialize the if stack with never.
                        # Until a conditional is evaluated,
                        # it is not true.
                        self.stack += [("if",TFN(Never))]
                    else:
                        # if [ a = b ] ;
                        #  7 6 5 4 3 2 1
                        # if [ a = b ] 
                        #  6 5 4 3 2 1 
                        testresult = self.evaltest(args)
                        if testresult is None:
                            pass #here(gr.dump())
                        self.stack += [("if",TFN(testresult))]
                elif args[0] == "fi":
                    self.stack = self.stack[:-1]
                g = re.match(r'(\w+)=(.*)', args[0])
                if g:
                    varname = g.group(1)
                    value = g.group(2)
                    #self.vars[varname] = value
                    self.set_var(varname, value)
                    return

            if len(self.stack) > 0:
                skip = not self.stack[-1][1]
            if len(self.case_stack) > 0:
                if not self.case_stack[-1][1]:
                    skip = True
            if len(self.for_loops)>0:
                f = self.for_loops[-1]
                if f.index >= len(f.values):
                    skip = True
            if skip:
                return []
            if len(args)==0:
                return []

            if args[0] == "exit":
                try:
                    rc = int(args[1])
                except:
                    rc = 1
                self.vars["?"] = str(rc)
                return []
            if args[0] == "wait":
                result = None
                try:
                    pid, status = os.wait()
                    p = get_running(pid)
                    result = p.communicate()
                except:
                    p = get_running(None)
                    if p is not None:
                        result = p.communicate()
                self.log("end wait:",self.vars["?"])
                return []
            if args[0] == "cd":
                if len(args) == 1:
                    if self.allow_cd(home):
                        os.chdir(home)
                else:
                    if self.allow_cd(args[1]):
                        os.chdir(args[1])
                self.log("chdir:",args[1])
                self.vars["PWD"] = os.getcwd()
                return

            if args[0] in self.funcs:
                # Invoke a function
                try:
                    save = {}
                    for vnum in range(1,1000): #self.max_args):
                        vname = str(vnum)
                        if vname in self.vars:
                            save[vname] = self.vars[vname]
                        else:
                            break
                    save["@"] = self.vars["@"]
                    for vnum in range(1,len(args)):
                        vname = str(vnum)
                        self.vars[vname] = args[vnum]
                    self.vars["@"] = " ".join(args[1:])
                    for c in self.funcs[args[0]]:
                        if c.is_("redir"):
                            continue
                        self.recursion += 1
                        try:
                            assert self.recursion < self.max_recursion_depth, f"Max recursion depth {self.max_recursion_depth} exceeded"
                            self.eval(c)
                        finally:
                            self.recursion -= 1
                finally:
                    for vnum in range(1,1000): #self.max_args):
                        vname = str(vnum)
                        if vname in self.vars:
                            save[vname] = self.vars[vname]
                        else:
                            break
                    for k in save:
                        self.vars[k] = save[k]
                return []
            elif args[0] not in ["if","then","else","fi","for","done","case","esac"]:
                sout = self.stdout
                serr = self.stderr
                sin = self.stdin
                if not os.path.exists(args[0]):
                    args0 = which(args[0])
                    if args0 is not None:
                        args0 = os.path.abspath(args0)
                        args[0] = args0
                if args[0] in ["/usr/bin/bash","/bin/bash","/usr/bin/sh","/bin/sh","source"]:
                    args = [sys.executable, my_shell] + args[1:]
                # We don't have a way to tell Popen we want both
                # streams to go to stderr, so we add this flag
                # and swap the output and error output after the
                # command is run
                out_is_error = False
                if redir is not None:
                    sout,serr,sin,out_is_error = self.do_redir(redir,sout,serr,sin)
                if len(args) == 0 or args[0] is None:
                    return ""
                if os.path.exists(args[0]):
                    try:
                        with open(args[0],"r") as fd:
                            first_line = fd.readline()
                            if first_line.startswith("#!"):
                                args = re.split(r'\s+',first_line[2:].strip()) + args
                    except:
                        pass
                if not os.path.exists(args[0]):
                    print(f"Command '{args[0]}' not found")
                    self.vars["?"] = 1
                    return ""
                if not self.allow_cmd(args):
                    return ""
                self.log("args:",args)
                env = {}
                for e in self.exports:
                    env[e] = self.vars[e]
                try:
                    p = PipeThread(args, stdin=sin, stdout=sout, stderr=serr, universal_newlines=True, env=env)
                except OSError as e:
                    args = ["/bin/sh"]+args
                    p = PipeThread(args, stdin=sin, stdout=sout, stderr=serr, universal_newlines=True, env=env)
                if self.curr_ending == "&":
                    p.background()
                    p.start()
                elif xending == "|":
                    p.setDaemon(True)
                    p.start()
                    return []
                else:
                    p.start()
                    o, e = p.communicate()
                    if out_is_error:
                        o, e = e, o
                    if type(o) == str:
                        self.output += o
                    if type(e) == str:
                        self.error += e
                    self.vars["?"] = str(p.returncode)
                    self.log("end cmd:",self.vars["?"],self.output,self.error)
                    return o
            return []
        finally:
            if self.curr_pipe is not None:
                self.stdout = self.save_out[-1]
                self.save_out = self.save_out[:-1]
            if self.last_pipe is not None:
                self.stdin = self.save_in[-1]
                self.save_in = self.save_in[:-1]
            self.last_ending = self.curr_ending
            self.last_pipe = self.curr_pipe

    def run_text(self,txt):
        #here(colored("="*50,"yellow"))
        #here(txt)
        self.log("txt:",txt)
        txt = self.txt + txt
        if txt.endswith("\\\n"):
            self.txt = txt
            return "CONTNUE"

        #print(colored(txt,"cyan"))
        m = Matcher(pp, "whole_cmd", txt)
        if m.matches():
            for gr in m.gr.children:
                if gr.is_("case"):
                    if not gr.has(-1,"casepattern"):
                        self.txt = txt+'\n'
                        return "CONTINUE"
                elif gr.is_("case2"):
                    if not gr.has(0):
                        self.txt = txt+'\n'
                        return "CONTINUE"
                else:
                    pass
            # here(m.gr.dump())
            if verbose:
                print(colored(txt,"cyan"))
                print(colored(m.gr.dump(),"magenta"))
            end = m.gr.end
            txt2 = txt[end:]
            if len(txt2)>0:
                s.run_text(txt2)
            self.txt = ''
            self.lines += [m.gr]
            self.eval(m.gr)
            if len(self.stack) > 0 or len(self.for_loops) > 0:
                return "EVALCONTINUE"
            else:
                return "EVAL"
        elif m.maxTextPos == len(txt):
            self.txt = txt
            #print()
            #m.showError()
            #print("continue...")
            self.log("CONTINUE")
            return "CONTNUE"
        else:
            self.txt = ''
            m.showError()
            #m.showError(sys.stderr)
            here("done")
            self.log("SYNTAX")
            m.showError(self.log_fd)
            return "SYNTAX"

def interactive(shell):
    try:
        import readline
    except:
        print(colored("Import of readline failed","red"))
    msg = "EVAL"
    while True:
        if msg == "EVAL":
            ps = colored('\U0001f370> ','yellow')
        else:
            ps = colored('\U0001f370? ','cyan')
        sys.stdout.flush()
        try:
            inp = input(ps)
            msg = shell.run_text(inp)
        except KeyboardInterrupt as ke:
            print(colored("Interrupt","red"))
        except EOFError as ee:
            return shell.vars["?"]

def run_shell(s):
    ssh_cmd = os.environ.get("SSH_ORIGINAL_COMMAND",None)
    if ssh_cmd is not None:
        try:
            rc = s.run_text(ssh_cmd)
            s.log("rc1:",rc)
        except:
            s.log_exc()
    elif len(sys.argv) == 1:
        try:
            rc = interactive(s)
            s.log("rc2:",rc)
        except SystemExit as se:
            rc = se.code
        except:
            rc = 1
            s.log_exc()
        exit(rc)
    else:
        for n in range(1,len(sys.argv)):
            f = sys.argv[n]
            if f == "-c":
                n += 1
                s.run_text(sys.argv[n])
            elif os.path.exists(f):
                with open(f,"r") as fd:
                    try:
                        rc = s.run_text(fd.read())
                        s.log("rc3:",rc)
                        assert rc == "EVAL", f"rc={rc}"
                    except SystemExit as sc:
                        # Calling exit sends you here
                        pass
                    except:
                        s.log_exc()

if __name__ == "__main__":
    s = shell()
    run_shell(s)
