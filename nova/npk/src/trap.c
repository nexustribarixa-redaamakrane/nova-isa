/*
 * NPK Milestone 3 trap dispatch.
 *
 * The assembly vector in trap.s saves the interrupted context into the fixed
 * software frame, then calls this file. Dispatch is not a conventional bypass
 * of NPK: every trap is admitted as an NPK_OP_TRAP_PROCESS operation and moves
 * through the same ADMIT -> VALIDATE -> EXECUTE -> COMMIT -> COMPLETE lifecycle.
 */

#include "npk_core.h"

#define NPK_TRAP_FRAME_ADDRESS 0x90900
#define NPK_TRAP_STATE_ADDRESS 0x90A40

#define NPK_TRAP_STATE_MAGIC 0x4E504B54
#define NPK_TRAP_STATE_VERSION 3

#define NPK_OP_TRAP_PROCESS 3

#define NPK_STAGE_NONE 0

#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_PU_IDLE 0

#define NPK_REASON_NONE 0
#define NPK_REASON_TRAP_BAD_FRAME 14
#define NPK_REASON_TRAP_BAD_ECODE 15
#define NPK_REASON_TRAP_UNSUPPORTED 16
#define NPK_REASON_TRAP_STATE 17

#define NPK_OK 0

#define NPK_CLASS_NONE 0
#define NPK_CLASS_SYSTEM_CALL 1
#define NPK_CLASS_SOFTWARE_TRAP 2
#define NPK_CLASS_ILLEGAL_INSTRUCTION 3
#define NPK_CLASS_ALIGNMENT_FAULT 4
#define NPK_CLASS_MEMORY_FAULT 5
#define NPK_CLASS_MMU_FAULT 6
#define NPK_CLASS_UNSUPPORTED 7

#define NPK_ACTION_NONE 0
#define NPK_ACTION_RESUME_NEXT 1
#define NPK_ACTION_REPORT_RESUME 2
#define NPK_ACTION_UNSUPPORTED 3

#define NPK_TRAP_RESULT_PROCESSED 0x54525058

#define NPK_TRAP_STATUS_OK 0
#define NPK_TRAP_STATUS_BAD_FRAME 1
#define NPK_TRAP_STATUS_BAD_STATE 2
#define NPK_TRAP_STATUS_ADMIT 3
#define NPK_TRAP_STATUS_ADVANCE 4

struct NpkTrapState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 dispatch_count;
    npk_u64 last_op_slot;
    npk_u64 last_status;
    npk_u64 last_ecode;
    npk_u64 last_ecause;
    npk_u64 last_epc;
    npk_u64 last_tval;
    npk_u64 classification;
    npk_u64 action;
    npk_u64 result;
    npk_u64 receipt_index;
    npk_u64 last_reason;
    npk_u64 reserved[2];
};

int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input, npk_u64 *out_slot);
int npk_core_advance(npk_u64 slot);
int npk_core_release(npk_u64 slot);
int npk_core_operation_outcome(npk_u64 slot);
npk_u64 npk_core_operation_result(npk_u64 slot);
npk_u64 npk_core_operation_reason(npk_u64 slot);
npk_u64 npk_core_operation_receipt_index(npk_u64 slot);

static struct NpkTrapFrame *npk_frame(void)
{
    return (struct NpkTrapFrame *)NPK_TRAP_FRAME_ADDRESS;
}

static struct NpkTrapState *npk_state(void)
{
    return (struct NpkTrapState *)NPK_TRAP_STATE_ADDRESS;
}

static npk_u64 npk_trap_classification(npk_u64 ecode)
{
    if (ecode == 7) {
        return NPK_CLASS_SYSTEM_CALL;
    }
    if (ecode == 10) {
        return NPK_CLASS_SOFTWARE_TRAP;
    }
    if (ecode == 1) {
        return NPK_CLASS_ILLEGAL_INSTRUCTION;
    }
    if (ecode == 4) {
        return NPK_CLASS_ALIGNMENT_FAULT;
    }
    if (ecode == 3) {
        return NPK_CLASS_MEMORY_FAULT;
    }
    if (ecode == 9) {
        return NPK_CLASS_MMU_FAULT;
    }
    return NPK_CLASS_UNSUPPORTED;
}

static npk_u64 npk_trap_action(npk_u64 classification)
{
    if (classification == NPK_CLASS_SYSTEM_CALL ||
            classification == NPK_CLASS_SOFTWARE_TRAP) {
        return NPK_ACTION_RESUME_NEXT;
    }
    if (classification == NPK_CLASS_UNSUPPORTED) {
        return NPK_ACTION_UNSUPPORTED;
    }
    return NPK_ACTION_REPORT_RESUME;
}

int npk_trap_init(void)
{
    struct NpkTrapState *state = npk_state();
    npk_u64 i;

    for (i = 0; i < 16; i = i + 1) {
        *((npk_u64 *)state + i) = 0;
    }
    state->magic = NPK_TRAP_STATE_MAGIC;
    state->version = NPK_TRAP_STATE_VERSION;
    state->last_op_slot = NPK_NO_INDEX;
    state->receipt_index = NPK_NO_INDEX;
    return NPK_OK;
}

npk_u64 npk_trap_validate(struct NpkOperation *op)
{
    struct NpkTrapFrame *frame;
    struct NpkTrapState *state = npk_state();

    if (op->input != NPK_TRAP_FRAME_ADDRESS) {
        return NPK_REASON_TRAP_BAD_FRAME;
    }
    if (state->magic != NPK_TRAP_STATE_MAGIC ||
            state->version != NPK_TRAP_STATE_VERSION) {
        return NPK_REASON_TRAP_STATE;
    }
    frame = npk_frame();
    if (frame->ecode == 0 || frame->ecode > 11) {
        return NPK_REASON_TRAP_BAD_ECODE;
    }
    if (frame->epc % 4 != 0 || frame->handler_priv != 2) {
        return NPK_REASON_TRAP_BAD_FRAME;
    }
    return NPK_REASON_NONE;
}

npk_u64 npk_trap_execute(struct NpkOperation *op)
{
    struct NpkTrapFrame *frame = npk_frame();
    struct NpkTrapState *state = npk_state();
    npk_u64 classification;

    classification = npk_trap_classification(frame->ecode);
    state->classification = classification;
    state->action = npk_trap_action(classification);
    if (classification == NPK_CLASS_UNSUPPORTED) {
        op->provisional = 0;
        return NPK_REASON_TRAP_UNSUPPORTED;
    }
    op->provisional = NPK_TRAP_RESULT_PROCESSED;
    return NPK_REASON_NONE;
}

npk_u64 npk_trap_commit(struct NpkOperation *op)
{
    if (op->provisional != NPK_TRAP_RESULT_PROCESSED) {
        return NPK_REASON_TRAP_STATE;
    }
    op->result = op->provisional;
    return NPK_REASON_NONE;
}

int npk_trap_dispatch(struct NpkTrapFrame *frame)
{
    struct NpkTrapState *state = npk_state();
    npk_u64 slot;
    npk_u64 id;
    npk_u64 i;
    int status;
    int outcome;
    int syscall_status = 0;
    int syscall_handoff = 0;

    if (frame != npk_frame()) {
        state->last_status = NPK_TRAP_STATUS_BAD_FRAME;
        return NPK_TRAP_STATUS_BAD_FRAME;
    }
    if (state->magic != NPK_TRAP_STATE_MAGIC ||
            state->version != NPK_TRAP_STATE_VERSION) {
        state->last_status = NPK_TRAP_STATUS_BAD_STATE;
        return NPK_TRAP_STATUS_BAD_STATE;
    }

    state->dispatch_count = state->dispatch_count + 1;
    state->last_ecode = frame->ecode;
    state->last_ecause = frame->ecause;
    state->last_epc = frame->epc;
    state->last_tval = frame->tval;
    /* Classification is a fact about the saved trap frame. Publish it before
     * core admission so Milestone 6 can still hand a SYSTEM_CALL to the syscall
     * service if the trap receipt log is already bounded and full. */
    state->classification = npk_trap_classification(frame->ecode);
    state->action = npk_trap_action(state->classification);
    state->result = 0;
    state->receipt_index = NPK_NO_INDEX;
    state->last_reason = NPK_REASON_NONE;

    id = state->dispatch_count;
    slot = NPK_NO_INDEX;
    status = npk_core_admit(NPK_OP_TRAP_PROCESS, id,
                            NPK_TRAP_FRAME_ADDRESS, &slot);
    if (status != NPK_OK) {
        /* A full trap log must not silently swallow a valid system call. The
         * syscall service has its own bounded log and publishes its own
         * NO_RESOURCE error when that log is full. */
        state->last_status = NPK_TRAP_STATUS_ADMIT;
        state->last_op_slot = NPK_NO_INDEX;
        syscall_handoff = 1;
    } else {
        for (i = 0; i < 5; i = i + 1) {
            outcome = npk_core_operation_outcome(slot);
            if (outcome != NPK_OUTCOME_PENDING) {
                break;
            }
            status = npk_core_advance(slot);
            if (status != NPK_OK) {
                state->last_status = NPK_TRAP_STATUS_ADVANCE;
                break;
            }
        }

        state->last_op_slot = slot;
        state->last_reason = npk_core_operation_reason(slot);
        state->result = npk_core_operation_result(slot);
        state->receipt_index = npk_core_operation_receipt_index(slot);
        if (status == NPK_OK) {
            outcome = npk_core_operation_outcome(slot);
            if (outcome == NPK_OUTCOME_COMPLETE) {
                state->last_status = NPK_TRAP_STATUS_OK;
                syscall_handoff = 1;
            } else {
                status = NPK_TRAP_STATUS_ADVANCE;
                state->last_status = status;
            }
        }
        npk_core_release(slot);
    }

    /* Milestone 6: a validated SYSTEM_CALL trap hands the saved frame to the
     * dedicated syscall subsystem, even if the M3 trap receipt log could not
     * admit its own operation. A trap operation that rejected or failed the
     * frame is not handed off. The syscall service owns a fresh core operation
     * and receipt; trap.s restores its frame results before MRET. */
    if (syscall_handoff == 1 &&
            state->classification == NPK_CLASS_SYSTEM_CALL &&
            frame->ecode == 7 && frame->epc % 4 == 0 &&
            frame->handler_priv == 2) {
        syscall_status = npk_syscall_dispatch(frame);
        state->reserved[0] = (npk_u64)syscall_status;
    }
    return status;
}
