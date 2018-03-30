# Copyright 2018 Cristian Mattarei
#
# Licensed under the modified BSD (3-clause BSD) License.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from six.moves import cStringIO

from pysmt.shortcuts import And, Or, Solver, TRUE, FALSE, Not, EqualsOrIff, Implies, Iff, Symbol, BOOL, get_free_variables, simplify
from pysmt.smtlib.printers import SmtPrinter, SmtDagPrinter
from pysmt.rewritings import conjunctive_partition
from pysmt.walkers.identitydag import IdentityDagWalker

from cosa.util.logger import Logger
from cosa.core.transition_system import TS, HTS
from cosa.encoders.coreir import CoreIRParser, SEP

from cosa.printers import TextTracePrinter, VCDTracePrinter

import copy

NL = "\n"

S1 = "sys1"+SEP
S2 = "sys2"+SEP

FWD = "FWD"
BWD = "BWD"
ZZ  = "ZZ"

class BMCConfig(object):

    incremental = True
    strategy = None
    solver = None
    full_trace = False
    prefix = None
    smt2file = None
    simplify = False
    map_function = None
    solver_name = None
    
    def __init__(self):
        self.incremental = True
        self.strategy = FWD
        self.solver_name = "z3"
        self.full_trace = False
        self.prefix = None
        self.smt2file = None
        self.simplify = False
        self.map_function = None
    
        self.strategies = BMCConfig.get_strategies()

    @staticmethod
    def get_strategies():
        strategies = []
        strategies.append((FWD, "Forward reachability"))
        strategies.append((BWD, "Backward reachability"))
        strategies.append((ZZ,  "Mixed Forward and Backward reachability (Zig-Zag)"))

        return strategies
            
class BMC(object):

    hts = None
    config = None

    TraceID = 0

    smtvars = None
    total_time = 0.0

    def __init__(self, hts, config):
        self.hts = hts
        self.config = config

        self.assert_property = False
        
        Logger.time = True
        self.total_time = 0.0

        if self.config.smt2file:
            self.smtvars = set([])
            with open(self.config.smt2file, "w") as f:
                f.write("(set-logic QF_BV)\n")


        self.solver = Solver(name=config.solver_name)
                
        self.subwalker = SubstituteWalker(invalidate_memoization=True)

    def __init_at_time(self, vars, maxtime):
        self.varmapf_t = {}
        self.varmapb_t = {}

        timed = TS.get_timed_name
        ptimed = TS.get_ptimed_name
        prime = TS.get_prime_name

        varsstr = [v.symbol_name() for v in vars]
        
        for t in range(maxtime+2):
            varmapf = []
            varmapb = []
            
            for sname in varsstr:
                psname = prime(sname)
                
                varmapf.append((sname, timed(sname, t)))
                varmapf.append((psname, timed(sname, t+1)))

                varmapb.append((sname, ptimed(sname, t)))
                varmapb.append((psname, ptimed(sname, t-1)))

            self.varmapf_t[t] = dict(varmapf)
            self.varmapb_t[t-1] = dict(varmapb)
        
    def at_time(self, formula, t):
        self.subwalker.set_substitute_map(self.varmapf_t[t])
        return self.subwalker.walk(formula)

    def at_ptime(self, formula, t):
        self.subwalker.set_substitute_map(self.varmapb_t[t])
        return self.subwalker.walk(formula)
    
    def unroll(self, trans, invar, k_end, k_start=0):
        Logger.log("Unroll from %s to %s"%(k_start, k_end), 2)

        fwd = k_start <= k_end
        time_function = self.at_time if fwd else self.at_ptime
        (k_start, k_end) = (min(k_start, k_end), max(k_start, k_end))

        formula = []
        t = k_start
        while t < k_end:
            to_t = t+1 if fwd else t
            formula.append(time_function(trans, t))
            formula.append(time_function(invar, to_t))
            Logger.log("Add trans, k=%s"%t, 2)
            t += 1
                    
        return And(formula)

    def print_trace(self, hts, model, length, xvars=None, diff_only=True, map_function=None):
        trace = []
        prevass = []

        full_trace = self.config.full_trace
        
        if Logger.level(1):
            diff_only = False
            full_trace = True
        
        if self.config.prefix is None:
            printer = TextTracePrinter()
            printer.extra_vars = xvars
            printer.diff_only = diff_only
            printer.full_trace = full_trace
            trace = printer.print_trace(hts, model, length, map_function)
            
            Logger.log(trace, 0)
        else:
            if Logger.level(1):
                timer = Logger.start_timer("Trace generation")

            printer = VCDTracePrinter()
            trace = printer.print_trace(hts, model, length, map_function)

            if Logger.level(1):
                Logger.stop_timer(timer)
            
            BMC.TraceID += 1
            trace_file = "%s-id_%s%s"%(self.config.prefix, BMC.TraceID, printer.get_file_ext())
            with open(trace_file, "w") as f:
                f.write(trace)

    def equivalence(self, hts2, k, symbolic_init):
        (htseq, t, model) = self.combined_system(hts2, k, symbolic_init, True)

        self.__init_at_time(htseq.vars, k)
        
        if t > -1:
            Logger.log("Systems are NOT equivalent", 0)
            self.print_trace(htseq, model, t, None, False, map_function=self.config.map_function)
        else:
            Logger.log("Systems are equivalent with k=%s"%k, 0)
            

    def fsm_check(self):
        (htseq, t, model) = self.combined_system(self.hts, 1, True, False)

        self.__init_at_time(htseq.vars, k)
        
        if t > -1:
            Logger.log("FSM is NOT deterministic", 0)            
            self.print_trace(htseq, model, t, None, False, map_function=self.config.map_function)
        else:
            Logger.log("FSM is deterministic", 0)
            
                
    def combined_system(self, hts2, k, symbolic_init, inc=True):
        htseq = HTS("eq")

        map1 = dict([(v, TS.get_prefix(v, S1)) for v in self.hts.vars]+[(TS.get_prime(v), TS.get_prefix(TS.get_prime(v), S1)) for v in self.hts.vars])
        map2 = dict([(v, TS.get_prefix(v, S2)) for v in self.hts.vars]+[(TS.get_prime(v), TS.get_prefix(TS.get_prime(v), S2)) for v in self.hts.vars])

        ts1_init = TRUE()
        ts2_init = TRUE()

        if not symbolic_init:
            ts1_init = self.hts.single_init().substitute(map1)
            ts2_init = hts2.single_init().substitute(map2)
        
        ts1 = TS(set([TS.get_prefix(v, S1) for v in self.hts.vars]),\
                 ts1_init,\
                 self.hts.single_trans().substitute(map1),\
                 self.hts.single_invar().substitute(map1))
        ts1.state_vars = set([TS.get_prefix(v, S1) for v in self.hts.state_vars])

        ts2 = TS(set([TS.get_prefix(v, S2) for v in hts2.vars]),\
                 ts2_init,\
                 hts2.single_trans().substitute(map2),\
                 hts2.single_invar().substitute(map2))
        ts2.state_vars = set([TS.get_prefix(v, S2) for v in hts2.state_vars])

        htseq.add_ts(ts1)
        htseq.add_ts(ts2)

        inputs = self.hts.inputs.intersection(hts2.inputs)
        outputs = self.hts.outputs.intersection(hts2.outputs)

        htseq.inputs = set([TS.get_prefix(v, S1) for v in self.hts.inputs]).union(set([TS.get_prefix(v, S2) for v in hts2.inputs]))
        htseq.outputs = set([TS.get_prefix(v, S1) for v in self.hts.outputs]).union(set([TS.get_prefix(v, S2) for v in hts2.outputs]))
        
        if symbolic_init:
            states = self.hts.state_vars.intersection(hts2.state_vars)
        else:
            states = []
            
        eqinputs = TRUE()
        eqoutputs = TRUE()
        eqstates = TRUE()
        
        for inp in inputs:
            eqinputs = And(eqinputs, EqualsOrIff(TS.get_prefix(inp, S1), TS.get_prefix(inp, S2)))

        for out in outputs:
            eqoutputs = And(eqoutputs, EqualsOrIff(TS.get_prefix(out, S1), TS.get_prefix(out, S2)))

        for svar in states:
            eqstates = And(eqstates, EqualsOrIff(TS.get_prefix(svar, S1), TS.get_prefix(svar, S2)))
            
        miter_out = Symbol("eq_S1_S2", BOOL)
        
        if symbolic_init:
            eqmiteroutputs = Iff(miter_out, Implies(eqstates, eqoutputs))
        else:
            eqmiteroutputs = Iff(miter_out, eqoutputs)

        htseq.add_ts(TS(set([miter_out]), TRUE(), TRUE(), And(eqinputs, eqmiteroutputs)))

        if inc:
            (t, model) = self.solve(htseq, miter_out, k)
            model = self.__remap_model(htseq.vars, model, k)
        else:
            (t, model) = self.solve_fwd(htseq, miter_out, k, False)

        return (htseq, t, model)


    def simulate(self, prop, k):
        self.__init_at_time(self.hts.vars, k)
        
        if prop == TRUE():
            self.config.incremental = False
            (t, model) = self.solve_fwd(self.hts, Not(prop), k, False)
        else:
            (t, model) = self.solve(self.hts, Not(prop), k)

        model = self.__remap_model(self.hts.vars, model, t)
        
        if t > -1:
            Logger.log("Execution found", 0)
            self.print_trace(self.hts, model, t, prop.get_free_variables(), map_function=self.config.map_function)
            return True
        else:
            Logger.log("Deadlock wit k=%s"%k, 0)
            return False

    def solve(self, hts, prop, k, k_min=0):
        if self.config.incremental:
            return self.solve_inc(hts, prop, k, k_min)

        return self.solve_fwd(hts, prop, k)
            
    def solve_inc(self, hts, prop, k, k_min):
        if self.config.strategy == FWD:
            return self.solve_inc_fwd(hts, prop, k, k_min)
        
        if self.config.strategy == BWD:
            return self.solve_inc_bwd(hts, prop, k)

        if self.config.strategy == ZZ:
            return self.solve_inc_zz(hts, prop, k)

        Logger.error("Invalid configuration strategy")
        
        return None

    def solve_fwd(self, hts, prop, k, shortest=True):

        init = hts.single_init()
        trans = hts.single_trans()
        invar = hts.single_invar()

        t_start = 0 if shortest else k
        
        t = 0 if shortest else k
        while (t < k+1):
            self.__reset_assertions(self.solver)

            formula = And(init, invar)
            formula = self.at_time(formula, 0)
            Logger.log("Add init and invar", 2)
            self.__add_assertion(self.solver, formula)

            trans_t = self.unroll(trans, invar, t)
            self.__add_assertion(self.solver, trans_t)
            
            propt = self.at_time(Not(prop), t)
            Logger.log("Add property time %d"%t, 2)
            self.__add_assertion(self.solver, propt)

            res = self.__solve(self.solver)

            if res:
                Logger.log("Counterexample found with k=%s"%(t), 1)
                model = self.solver.get_model()
                Logger.log("", 0, not(Logger.level(1)))
                return (t, model)
            else:
                Logger.log("No counterexample found with k=%s"%(t), 1)
                Logger.msg(".", 0, not(Logger.level(1)))
                
            t += 1
        Logger.log("", 0, not(Logger.level(1)))

        Logger.error("Invalid configuration strategy")        
        return (-1, None)
    
    def solve_inc_fwd(self, hts, prop, k, k_min):
        self.__reset_assertions(self.solver)

        init = hts.single_init()
        trans = hts.single_trans()
        invar = hts.single_invar()

        if self.config.simplify:
            Logger.log("Simplifying the Transition System", 1)
            if Logger.level(1):
                timer = Logger.start_timer("Simplify")

            init = simplify(init)
            trans = simplify(trans)
            invar = simplify(invar)

            if Logger.level(1):
                Logger.stop_timer(timer)

        propt = FALSE()
        formula = And(init, invar)
        formula = self.at_time(formula, 0)
        Logger.log("Add init and invar", 2)
        self.__add_assertion(self.solver, formula)

        next_prop = TS.has_next(prop)
        if next_prop:
            if k < 1:
                Logger.error("Invariant checking with next variables requires at least k=1")
            k_min = 1
        
        t = 0 
        while (t < k+1):
            self.__push(self.solver)

            if k_min > 0:
                t_prop = t-1 if next_prop else t
                if (not next_prop) or (next_prop and t>0):
                    propt = Or(propt, self.at_time(Not(prop), t_prop))
            else:
                propt = self.at_time(Not(prop), t)
                
            Logger.log("Add not property at time %d"%t, 2)
            self.__add_assertion(self.solver, propt)

            if t >= k_min:
                Logger.log("Solving for k=%s"%(t), 1)

                res = self.__solve(self.solver)

                if res:
                    Logger.log("Counterexample found with k=%s"%(t), 1)
                    model = self.solver.get_model()
                    Logger.log("", 0, not(Logger.level(1)))
                    return (t, model)
                else:
                    Logger.log("No counterexample found with k=%s"%(t), 1)
                    Logger.msg(".", 0, not(Logger.level(1)))
            else:
                Logger.log("Skipping solving for k=%s (k_min=%s)"%(t,k_min), 1)
                Logger.msg(".", 0, not(Logger.level(1)))
                    
            self.__pop(self.solver)

            trans_t = self.unroll(trans, invar, t+1, t)
            self.__add_assertion(self.solver, trans_t)
            
            if self.assert_property:
                prop_t = self.unroll(TRUE(), prop, t, t-1)
                self.__add_assertion(self.solver, prop_t)
                Logger.log("Add property at time %d"%t, 2)
                
            t += 1
        Logger.log("", 0, not(Logger.level(1)))
                
        return (-1, None)
    
    def solve_inc_bwd(self, hts, prop, k):
        self.__reset_assertions(self.solver)

        if TS.has_next(prop):
            Logger.error("Invariant checking with next variables only supports FWD strategy")
        
        init = hts.single_init()
        trans = hts.single_trans()
        invar = hts.single_invar()

        formula = self.at_ptime(And(Not(prop), invar), -1)
        Logger.log("Add not property at time %d"%0, 2)
        self.__add_assertion(self.solver, formula)

        t = 0 
        while (t < k+1):
            self.__push(self.solver)

            pinit = self.at_ptime(init, t-1)
            Logger.log("Add init at time %d"%t, 2)
            self.__add_assertion(self.solver, pinit)

            res = self.__solve(self.solver)

            if res:
                Logger.log("Counterexample found with k=%s"%(t), 1)
                model = self.solver.get_model()
                Logger.log("", 0, not(Logger.level(1)))
                return (t, model)
            else:
                Logger.log("No counterexample found with k=%s"%(t), 1)
                Logger.msg(".", 0, not(Logger.level(1)))

            self.__pop(self.solver)
            
            trans_t = self.unroll(trans, invar, t, t+1)
            self.__add_assertion(self.solver, trans_t)

            if self.assert_property and t > 0:
                prop_t = self.unroll(TRUE(), prop, t-1, t)
                self.__add_assertion(self.solver, prop_t)
                Logger.log("Add property at time %d"%t, 2)
            
            t += 1
        Logger.log("", 0, not(Logger.level(1)))
                
        return (-1, None)
        
    def solve_inc_zz(self, hts, prop, k):
        self.__reset_assertions(self.solver)
        
        if TS.has_next(prop):
            Logger.error("Invariant checking with next variables only supports FWD strategy")

        init = hts.single_init()
        trans = hts.single_trans()
        invar = hts.single_invar()

        initt = self.at_time(And(init, invar), 0)
        Logger.log("Add init at_0", 2)
        self.__add_assertion(self.solver, initt)
        
        propt = self.at_ptime(And(Not(prop), invar), -1)
        Logger.log("Add property pat_%d"%0, 2)
        self.__add_assertion(self.solver, propt)
        
        t = 0 
        while (t < k+1):
            self.__push(self.solver)
            even = (t % 2) == 0
            th = int(t/2)

            if even:
                eq = And([EqualsOrIff(self.at_time(v, th), self.at_ptime(v, th-1)) for v in hts.vars])
            else:
                eq = And([EqualsOrIff(self.at_time(v, th+1), self.at_ptime(v, th-1)) for v in hts.vars])
                
            Logger.log("Add equivalence time %d"%t, 2)
            self.__add_assertion(self.solver, eq)

            res = self.__solve(self.solver)

            if res:
                Logger.log("Counterexample found with k=%s"%(t), 1)
                model = self.solver.get_model()
                Logger.log("", 0, not(Logger.level(1)))
                return (t, model)
            else:
                Logger.log("No counterexample found with k=%s"%(t), 1)
                Logger.msg(".", 0, not(Logger.level(1)))

            self.__pop(self.solver)

            if even: 
                trans_t = self.unroll(trans, invar, th+1, th)
            else:
                trans_t = self.unroll(trans, invar, th, th+1)

            self.__add_assertion(self.solver, trans_t)
                
            t += 1
        Logger.log("", 0, not(Logger.level(1)))
                
        return (-1, None)
            
    def safety(self, prop, k, k_min):
        self.__init_at_time(self.hts.vars, k)
        (t, model) = self.solve(self.hts, prop, k, k_min)

        model = self.__remap_model(self.hts.vars, model, t)
        
        if t > -1:
            Logger.log("Property is FALSE", 0)
            self.print_trace(self.hts, model, t, prop.get_free_variables(), map_function=self.config.map_function)
            return False
        else:
            Logger.log("No counterexample found", 0)
            return True

    def __remap_model(self, vars, model, k):
        if model is None:
            return model
        
        if self.config.strategy == BWD:
            return self.__remap_model_bwd(vars, model, k)

        if self.config.strategy == ZZ:
            return self.__remap_model_zz(vars, model, k)

        if self.config.strategy == FWD:
            return self.__remap_model_fwd(vars, model, k)

        Logger.error("Invalid configuration strategy")
        return None
        
    def __remap_model_fwd(self, vars, model, k):
        return model

    def __remap_model_bwd(self, vars, model, k):
        retmodel = dict()
        
        for var in vars:
            for t in range(k+1):
                retmodel[TS.get_timed(var, t)] = model[TS.get_ptimed(var, k-t)]

        return retmodel

    def __remap_model_zz(self, vars, model, k):
        retmodel = dict(model)

        for var in vars:
            for t in range(int(k/2)+1, k+1, 1):
                retmodel[TS.get_timed(var, t)] = model[TS.get_ptimed(var, k-t)]
                
        return retmodel

    def __write_smt2_log(self, line):
        if self.config.smt2file is not None:
            with open(self.config.smt2file, "a") as f:
                f.write(line+"\n")
    
    def __add_assertion(self, solver, formula):
        if not self.config.skip_solving:
            solver.add_assertion(formula)
            
        if Logger.level(3):
            buf = cStringIO()
            printer = SmtPrinter(buf)
            printer.printer(formula)
            print(buf.getvalue()+"\n")

        if self.config.smt2file is not None:
            for v in set(formula.get_free_variables()).difference(self.smtvars):
                if v.symbol_type() == BOOL:
                    self.__write_smt2_log("(declare-fun %s () Bool)" % (v.symbol_name()))
                else:
                    self.__write_smt2_log("(declare-fun %s () (_ BitVec %s))" % (v.symbol_name(), v.symbol_type().width))

            self.__write_smt2_log("")
            self.smtvars = set(formula.get_free_variables()).union(self.smtvars)

            if formula.is_and():
                for f in conjunctive_partition(formula):
                    buf = cStringIO()
                    printer = SmtPrinter(buf)
                    printer.printer(f)
                    self.__write_smt2_log("(assert %s)"%buf.getvalue())
            else:
                buf = cStringIO()
                printer = SmtPrinter(buf)
                printer.printer(formula)
                self.__write_smt2_log("(assert %s)"%buf.getvalue())
                    
                    
    def __push(self, solver):
        if not self.config.skip_solving:
            solver.push()

        self.__write_smt2_log("(push 1)")
        
    def __pop(self, solver):
        if not self.config.skip_solving:
            solver.pop()

        self.__write_smt2_log("(pop 1)")

    def __reset_assertions(self, solver):
        if not self.config.skip_solving:
            solver.reset_assertions()

        if self.config.smt2file is not None:
            with open(self.config.smt2file, "w") as f:
                f.write("(set-logic QF_BV)\n")

    def __solve(self, solver):
        self.__write_smt2_log("(check-sat)")
        self.__write_smt2_log("")
        
        if self.config.skip_solving:
            return None
            
        if Logger.level(1):
            timer = Logger.start_timer("Solve")

        r = solver.solve()
        
        if Logger.level(1):
            self.total_time += Logger.stop_timer(timer)
            Logger.log("Total time: %.2f sec"%self.total_time, 1)
            
        return r

class SubstituteWalker(IdentityDagWalker):

    def set_substitute_function(self, function):
        self.substitute_function = function
    
    def set_substitute_map(self, smap):
        self.mapsymbols = smap
    
    def walk_symbol(self, formula, args, **kwargs):
        if formula.symbol_name() in self.mapsymbols:
            return self.mgr.Symbol(self.mapsymbols[formula.symbol_name()],
                                   formula.symbol_type())
        else:
            return self.mgr.Symbol(formula.symbol_name(),
                                   formula.symbol_type())

