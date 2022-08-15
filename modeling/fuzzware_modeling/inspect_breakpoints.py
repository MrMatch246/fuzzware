import angr, claripy

from .angr_utils import contains_var, is_mmio_address, is_ast_mmio_address
from .arch_specific.arm_thumb_regs import newly_added_constraints_reg_names, REG_NAME_SP

import logging
l = logging.getLogger("MMIO")

#def inspect_bp_intercept_newly_added_constraints(state):
#    for i in range(len(state.inspect.added_constraints)):
#        # l.warning(f"before added constraints: {state.inspect.added_constraints[i]}")
#        # state.inspect.added_constraints[i] = None
#    return 1

#def inspect_bp_before_(state):
#    for i in range(len(state.inspect.added_constraints)):
#        l.warning(f"before added constraints: {state.inspect.added_constraints[i]}")
#        # state.inspect.added_constraints[i] = None
#    return 1

def inspect_after_address_concretization(state):
    if state.inspect.address_concretization_result is None:
        try:
            concretized_expr = state.liveness.base_snapshot.concretize(state.inspect.address_concretization_expr)
            l.info(f"[inspect_address_concretization AFTER] address expression '{state.inspect.address_concretization_expr}' concretized to None at pc: {state.addr:08x}, trying to intervene.\n Concretized to '{concretized_expr}'")
            state.inspect.address_concretization_result = [state.solver.eval_one(concretized_expr)]
            l.info(f"[inspect_address_concretization AFTER] Successfully overwrote to {state.inspect.address_concretization_result[0]:08x}")
        except Exception as e:
            l.warning(f"[inspect_address_concretization AFTER] Failed to concretize address. Error: {e}.\n bailing out")
            state.inspect.address_concretization_result = None

def inspect_bp_track_newly_added_constraints(state):
    """
    Whenever a constraint is added, check whether this fixes a variable to a specific value.
    If a variable now has a specific value, replace the register contents with the value.
    This allows killing references as early as possible.
    """
    symbolic_regs = [reg_name for reg_name in newly_added_constraints_reg_names if getattr(state.regs, reg_name).symbolic]
    if not symbolic_regs:
        return

    for constraint in state.inspect.added_constraints:
        for var in state.liveness.tracked_vars:
            if contains_var(constraint, var):
                for reg_name, reg_contents in map(lambda name: (name, getattr(state.regs, name)), symbolic_regs):
                    if contains_var(reg_contents, var):
                        unconstrained_reg_contents = state.liveness.base_snapshot.unconstrain(reg_contents)
                        simplified_reg_contents = state.solver.simplify(unconstrained_reg_contents)
                        try:
                            if state.solver.unique(simplified_reg_contents, extra_constraints=state.solver.constraints):
                                concrete_val = state.solver.eval_one(simplified_reg_contents, extra_constraints=state.solver.constraints)

                                print("Newly added constraint found in register {}: {}, constraint: {}. Only one value left: 0x{:x}. Overriding...".format(reg_name, reg_contents, constraint, concrete_val))
                                # Override register value with concrete value. This will trigger liveness counter updates via inspect bps
                                setattr(state.regs, reg_name, claripy.BVV(concrete_val, 32))
                        except angr.errors.SimUnsatError:
                            # If the solver can not handle one thing, do not bother anymore
                            return

def inspect_bp_trace_ret(state):
    # l.warning("At exit inspect breakpoint from {:x} to {}, jumpkind: {}, guard: {}".format(state.addr, state.inspect.exit_target, state.inspect.exit_jumpkind, state.inspect.exit_guard))

    # ret: decrement call depths
    if state.inspect.exit_jumpkind == "Ijk_Ret":
        # We cannot seem to use a "return" bp here as these are not hit
        # when the returns errors (which happens for us for interrupt returns)
        state.liveness.leave_function(state)

def inspect_bp_trace_call(state):
    # if state.inspect.exit_jumpkind == "Ijk_Call":
    l.debug("Calling into 0x{:08x}".format(state.addr))
    if state.globals['path_constrained']:
        state.globals['meaningful_actions_while_constrained'] = True
    state.liveness.enter_function(state)

def inspect_bp_trace_liveness_reg(state):
    if state.inspect.reg_write_offset not in state.globals['regular_reg_offsets']:
        return

    state.liveness.on_before_reg_write(state.inspect.reg_write_expr, state.inspect.reg_write_offset, state.inspect.reg_write_length)

def inspect_bp_trace_liveness_mem(state):
    try:
        addr = state.solver.eval_one(state.inspect.mem_write_address)

    except Exception as e:
        write_addrs = state.solver.eval_upto(state.inspect.mem_write_address, 300)
        history = [i for i in state.history.actions]
        history_log = ""
        # history_log="\n".join([f'Addr:{i.addr} Size:{i.size} Data:{i.data} Cond:{i.condition}' for i in history])
        for action in history:
            if "SimActionData" in str(action):
                history_log += f'\nAction: {action.action} Addr:{action.addr} Size:{action.size} Data:{action.data} Cond:{action.condition}'
            else:
                history_log += f'\n{action}'
        raise Exception(f"Got more than one MEM_WRITE_ADDR {len(write_addrs)} from state:\n {state}\n"
                        f"ALL:\n{[hex(addr) for addr in state.history.bbl_addrs]}\nLAST:\n{[hex(addr) for addr in state.history.recent_bbl_addrs]}"
                        f'\nActions taken:\n{history_log}')

    if addr == state.liveness.base_snapshot.mmio_addr and len(state.liveness.tracked_vars) == 1 and contains_var(state.inspect.mem_write_expr, state.liveness.tracked_vars[0]):
        l.info(f"config_write_performed set for state: {state}, written expression: {state.inspect.mem_write_expr}, first tracked variable: {state.liveness.tracked_vars[0]}")
        # We have a write to the mmio address which we originally read from: keep this as a note for config model detection
        state.globals['config_write_performed'] = True
        # The tracked address also is an MMIO address, not relevant for liveness tracking
        return
    elif is_mmio_address(state, addr):
        return
    elif state.globals['path_constrained']:
        state.globals['meaningful_actions_while_constrained'] = True

    if state.inspect.mem_write_address.symbolic and contains_var(state.inspect.mem_write_address, state.liveness.base_snapshot.regvars_by_name[REG_NAME_SP]):
        # Write to local variable
        l.debug("[{:x}] Write to local variable!".format(state.addr))
        l.debug("Target: {}, val: {}".format(state.inspect.mem_write_address, state.inspect.mem_write_expr))

        state.liveness.on_before_stack_mem_write(addr, state.inspect.mem_write_expr, state.inspect.mem_write_length)
    else:
        value_variable_names = [e._encoded_name for e in state.inspect.mem_write_expr.leaf_asts() if e.symbolic]
        # We got a write to memory outside of stack/MMIO. Check whether we are writing something that depends on tracked MMIO inputs
        for ast in state.liveness.tracked_vars:
            if ast._encoded_name in value_variable_names:
                # We are writing an MMIO input to the environment. Bail out
                l.warning(f"Writing MMIO input to ENV @ {state.inspect.mem_write_address} at PC {state.addr} ")
                #l.warning("[{:x}] MMIO value {} written out to the environment ([{:x}]={})".format(state.addr, ast, addr, state.inspect.mem_write_expr))
                state.globals['dead_write_to_env'] = True

def inspect_cond_is_mmio_read(state):
    return is_ast_mmio_address(state, state.inspect.mem_read_address)

def inspect_bp_mmio_intercept_read_after(state):
    # Ignore symbolic MMIO addresses
    try:
        state.solver.eval_one(state.inspect.mem_read_address)
    except Exception as e:
        print(f"inspect_bp_mmio_intercept_read_after error for addr {state.inspect.mem_read_address}: {e}")
        return

    state.liveness.on_after_mmio_mem_read(state.inspect.mem_read_address, state.inspect.mem_read_expr, state.inspect.mem_read_length)

def inspect_bp_singleton_ensure_mmio(state):
    read_addr = state.solver.eval(state.inspect.mem_read_address)
    if not is_mmio_address(state, read_addr):
        start = read_addr & (~0xfff)
        l.warning("Adding non-configured MMIO page at: 0x{:08x}".format(start))
        state.liveness.base_snapshot.add_custom_mmio_range(start, start + 0x1000)

    state.liveness.base_snapshot.access_pc = state.addr
    state.liveness.base_snapshot.mmio_addr = read_addr
    state.liveness.base_snapshot.mmio_access_size = state.inspect.mem_read_length
    l.warning(f"Found first MMIO access from {state.liveness.base_snapshot.access_pc:08x} to address: {read_addr:08x}")

    state.inspect.remove_breakpoint("mem_read", state.globals['tmp_mmio_bp'])

def inspect_bp_trace_reads(state):
    is_mmio_access = is_ast_mmio_address(state, state.inspect.mem_read_address)
    print('Read', state.inspect.mem_read_expr, 'at', state.inspect.mem_read_address, "from ", hex(state.addr),  "is mmio read? ->", is_mmio_access)

def inspect_bp_trace_writes(state):
    is_mmio_access = is_ast_mmio_address(state, state.inspect.mem_write_address)
    print('Write', state.inspect.mem_write_expr, 'to', state.inspect.mem_write_address, "from ", hex(state.addr),  "is mmio write? ->", is_mmio_access)
