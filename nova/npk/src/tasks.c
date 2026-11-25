/*
 * NPK Milestone 8 task management and cooperative scheduling.
 *
 * Tasks are typed kernel-managed records scheduled cooperatively through the
 * existing trap/syscall path. NPK records TCB state, a deterministic bounded
 * ready queue, and task receipts through the normal processing lifecycle.
 *
 * Context-switch model: Nova MRET restores EPC/privilege from the emulator's
 * hidden exception stack, not from the software trap frame. NPK therefore cannot
 * redirect return by editing frame.epc alone. For cooperative syscall resumption,
 * the scheduler patches the suspended task's return site with a JMP-r15
 * trampoline, restores the selected task's software frame into the live trap
 * frame, and sets r15 to the selected task resume PC. trap.s then executes MRET,
 * which returns to the patched return site and executes the trampoline.
 *
 * Limitations: this is cooperative single-address-space scheduling over the
 * existing single-level trap frame; it requires writable return sites; it does
 * not provide preemption, SMP, process isolation, or user-loader semantics.
 */
#include "npk_core.h"

#define NPK_TRAP_FRAME_ADDRESS 0x90900
#define NPK_TASK_STATE_ADDRESS 0xB0010
#define NPK_TASK_REQUEST_ADDRESS 0xB0090
#define NPK_TASK_RESPONSE_ADDRESS 0xB00F0
#define NPK_TASK_TABLE_ADDRESS 0xB0130
#define NPK_TASK_READY_QUEUE_ADDRESS 0xB1D30

#define NPK_TASK_STATE_MAGIC 0x4E504B4B
#define NPK_TASK_STATE_VERSION 1
#define NPK_TASK_REQUEST_MAGIC 0x4E504B51
#define NPK_TASK_RESPONSE_MAGIC 0x4E504B52

#define NPK_TASK_SLOTS 8
/* 17 scalar words + 15 reserved + 38 frame words = 560 bytes; stride 576
 * leaves 16 bytes of tail padding while keeping the frame at word 32. */
#define NPK_TASK_TCB_SIZE 576
#define NPK_TASK_FRAME_WORDS 38

#define NPK_TASK_FREE 0
#define NPK_TASK_CREATED 1
#define NPK_TASK_READY 2
#define NPK_TASK_RUNNING 3
#define NPK_TASK_BLOCKED 4
#define NPK_TASK_TERMINATED 5

#define NPK_OP_TASK_CREATE 31
#define NPK_OP_TASK_TERMINATE 32
#define NPK_OP_TASK_YIELD 33
#define NPK_OP_TASK_BLOCK 34
#define NPK_OP_TASK_WAKE 35
#define NPK_OP_TASK_QUERY 36
#define NPK_OP_TASK_DESTROY 37

#define NPK_REASON_NONE 0
#define NPK_REASON_TASK_BAD_REQUEST 78
#define NPK_REASON_TASK_BAD_ID 79
#define NPK_REASON_TASK_BAD_STATE 80
#define NPK_REASON_TASK_NOT_FOUND 81
#define NPK_REASON_TASK_DUPLICATE 82
#define NPK_REASON_TASK_EXHAUSTED 83
#define NPK_REASON_TASK_BAD_ENTRY 84
#define NPK_REASON_TASK_BAD_STACK 85
#define NPK_REASON_TASK_BAD_CONTEXT 86
#define NPK_REASON_TASK_BAD_TRANSITION 87
#define NPK_REASON_TASK_TERMINATED 88
#define NPK_REASON_TASK_NOT_RUNNING 89
#define NPK_REASON_TASK_NOT_BLOCKED 90
#define NPK_REASON_TASK_NOT_READY 91
#define NPK_REASON_TASK_UNSUPPORTED 92
#define NPK_REASON_TASK_STALE 93

#define NPK_STATUS_OK 0
#define NPK_STATUS_BAD_REQUEST 1
#define NPK_STATUS_ADMIT_FAIL 2
#define NPK_STATUS_ADVANCE_FAIL 3
#define NPK_STATUS_BAD_OUTCOME 4

#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_TASK_ID_BASE 0x5441534B00000000ULL
#define NPK_TASK_NO_INDEX 0xFFFFFFFFFFFFFFFFULL
#define NPK_JMP_R15 0x50780000ULL

struct NpkTaskState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 task_slots;
    npk_u64 task_count;
    npk_u64 ready_count;
    npk_u64 blocked_count;
    npk_u64 terminated_count;
    npk_u64 current_task;
    npk_u64 current_index;
    npk_u64 next_task_id;
    npk_u64 dispatch_count;
    npk_u64 switch_count;
    npk_u64 last_status;
    npk_u64 last_reason;
    npk_u64 last_task;
};

struct NpkTaskTcb {
    npk_u64 id;
    npk_u64 generation;
    npk_u64 state;
    npk_u64 owner;
    npk_u64 parent;
    npk_u64 entry;
    npk_u64 stack_base;
    npk_u64 stack_size;
    npk_u64 stack_top;
    npk_u64 arg;
    npk_u64 address_space;
    npk_u64 exit_status;
    npk_u64 resume_pc;
    npk_u64 resume_patch_addr;
    npk_u64 resume_patch_word;
    npk_u64 resume_patch_active;
    npk_u64 flags;
    npk_u64 reserved[15];
    npk_u64 frame[NPK_TASK_FRAME_WORDS];
};

struct NpkTaskRequest {
    npk_u64 magic;
    npk_u64 operation;
    npk_u64 id;
    npk_u64 task;
    npk_u64 arg0;
    npk_u64 arg1;
    npk_u64 arg2;
    npk_u64 arg3;
    npk_u64 arg4;
    npk_u64 arg5;
    npk_u64 arg6;
    npk_u64 arg7;
};

struct NpkTaskResponse {
    npk_u64 magic;
    npk_u64 status;
    npk_u64 operation;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 task;
    npk_u64 receipt_index;
    npk_u64 slot;
};

struct NpkTaskReadyEntry {
    npk_u64 task_id;
    npk_u64 task_index;
};

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);
extern void npk_core_set_task_arguments(npk_u64 slot, npk_u64 task_id,
                                        npk_u64 parent, npk_u64 entry,
                                        npk_u64 stack, npk_u64 stack_size,
                                        npk_u64 arg, npk_u64 address_space);

static struct NpkTaskState *npk_task_state(void) { return (struct NpkTaskState *)NPK_TASK_STATE_ADDRESS; }
static struct NpkTaskTcb *npk_task_tcb(npk_u64 i) { return (struct NpkTaskTcb *)(NPK_TASK_TABLE_ADDRESS + i * NPK_TASK_TCB_SIZE); }
static struct NpkTaskReadyEntry *npk_task_ready(npk_u64 i) { return (struct NpkTaskReadyEntry *)(NPK_TASK_READY_QUEUE_ADDRESS + i * sizeof(struct NpkTaskReadyEntry)); }
static struct NpkTaskRequest *npk_task_req(void) { return (struct NpkTaskRequest *)NPK_TASK_REQUEST_ADDRESS; }
static struct NpkTaskResponse *npk_task_resp(void) { return (struct NpkTaskResponse *)NPK_TASK_RESPONSE_ADDRESS; }
static struct NpkTrapFrame *npk_task_frame(void) { return (struct NpkTrapFrame *)NPK_TRAP_FRAME_ADDRESS; }

static void npk_task_copy_frame_to_tcb(struct NpkTaskTcb *task, struct NpkTrapFrame *frame)
{
    npk_u64 i;
    for (i = 0; i < 32; i = i + 1) task->frame[i] = frame->gpr[i];
    task->frame[32] = frame->psr; task->frame[33] = frame->epc;
    task->frame[34] = frame->ecause; task->frame[35] = frame->ecode;
    task->frame[36] = frame->handler_priv; task->frame[37] = frame->tval;
}

static void npk_task_copy_tcb_to_frame(struct NpkTrapFrame *frame, struct NpkTaskTcb *task)
{
    npk_u64 i;
    for (i = 0; i < 32; i = i + 1) frame->gpr[i] = task->frame[i];
    frame->psr = task->frame[32]; frame->epc = task->frame[33];
    frame->ecause = task->frame[34]; frame->ecode = task->frame[35];
    frame->handler_priv = task->frame[36]; frame->tval = task->frame[37];
}

static npk_u64 npk_task_find_index(npk_u64 task_id)
{
    npk_u64 i;
    struct NpkTaskTcb *task;
    if (task_id == 0) {
        return NPK_TASK_NO_INDEX;
    }
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1) {
        task = npk_task_tcb(i);
        if (task->id == task_id && task->state != NPK_TASK_FREE) {
            return i;
        }
    }
    return NPK_TASK_NO_INDEX;
}

static npk_u64 npk_task_find_live_index(npk_u64 task_id)
{
    npk_u64 i;
    struct NpkTaskTcb *task;
    if (task_id == 0) {
        return NPK_TASK_NO_INDEX;
    }
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1) {
        task = npk_task_tcb(i);
        if (task->id == task_id &&
                task->state != NPK_TASK_FREE &&
                task->state != NPK_TASK_TERMINATED) {
            return i;
        }
    }
    return NPK_TASK_NO_INDEX;
}

static npk_u64 npk_task_find_free(void)
{
    npk_u64 i;
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1)
        if (npk_task_tcb(i)->state == NPK_TASK_FREE) return i;
    return NPK_TASK_NO_INDEX;
}

static int npk_task_ready_contains(npk_u64 task_id)
{
    npk_u64 i; struct NpkTaskState *state = npk_task_state();
    for (i = 0; i < state->ready_count; i = i + 1)
        if (npk_task_ready(i)->task_id == task_id) return 1;
    return 0;
}

static int npk_task_ready_push(npk_u64 task_id, npk_u64 index)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskReadyEntry *entry;
    if (state->ready_count >= NPK_TASK_SLOTS) return 0;
    if (npk_task_ready_contains(task_id)) return 1;
    entry = npk_task_ready(state->ready_count);
    entry->task_id = task_id; entry->task_index = index;
    state->ready_count = state->ready_count + 1;
    return 1;
}

static npk_u64 npk_task_ready_pop(void)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 i, id;
    if (state->ready_count == 0) return NPK_TASK_NO_INDEX;
    id = npk_task_ready(0)->task_id;
    for (i = 1; i < state->ready_count; i = i + 1) *npk_task_ready(i - 1) = *npk_task_ready(i);
    state->ready_count = state->ready_count - 1;
    npk_task_ready(state->ready_count)->task_id = 0;
    npk_task_ready(state->ready_count)->task_index = NPK_TASK_NO_INDEX;
    return id;
}

static npk_u64 npk_task_ready_pop_resumable(void)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 i, count = state->ready_count;
    for (i = 0; i < count; i = i + 1) {
        npk_u64 cand_id = npk_task_ready_pop();
        npk_u64 cand_idx = npk_task_find_index(cand_id);
        struct NpkTaskTcb *cand = 0;
        if (cand_idx != NPK_TASK_NO_INDEX) cand = npk_task_tcb(cand_idx);
        if (cand != 0 && cand->state == NPK_TASK_READY && cand->flags == 1) return cand_id;
        if (cand != 0 && cand->state == NPK_TASK_READY) npk_task_ready_push(cand_id, cand_idx);
    }
    return NPK_TASK_NO_INDEX;
}

static void npk_task_refresh_counts(void)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 i, count = 0, blocked = 0, terminated = 0;
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1) {
        struct NpkTaskTcb *task = npk_task_tcb(i);
        if (task->state != NPK_TASK_FREE) count = count + 1;
        if (task->state == NPK_TASK_BLOCKED) blocked = blocked + 1;
        if (task->state == NPK_TASK_TERMINATED) terminated = terminated + 1;
    }
    state->task_count = count; state->blocked_count = blocked; state->terminated_count = terminated;
}

static void npk_task_set_state(struct NpkTaskTcb *task, npk_u64 new_state)
{
    task->state = new_state;
    npk_task_refresh_counts();
}

static int npk_task_apply_resume_patch(struct NpkTaskTcb *task)
{
    unsigned int *patch;
    if (task->resume_patch_active != 0) {
        return 1;
    }
    if (task->resume_patch_addr == 0 || (task->resume_patch_addr & 3) != 0) {
        return 0;
    }
    patch = (unsigned int *)(npk_u64)task->resume_patch_addr;
    task->resume_patch_word = (npk_u64)(*patch);
    *patch = (unsigned int)NPK_JMP_R15;
    task->resume_patch_active = 1;
    return 1;
}

static void npk_task_clear_resume_patch(struct NpkTaskTcb *task)
{
    unsigned int *patch;
    if (task->resume_patch_active == 0) {
        return;
    }
    if (task->resume_patch_addr != 0 && (task->resume_patch_addr & 3) == 0) {
        patch = (unsigned int *)(npk_u64)task->resume_patch_addr;
        *patch = (unsigned int)task->resume_patch_word;
    }
    task->resume_patch_active = 0;
    task->resume_patch_addr = 0;
    task->resume_patch_word = 0;
}

static void npk_task_init_frame(struct NpkTaskTcb *task)
{
    npk_u64 i;
    for (i = 0; i < NPK_TASK_FRAME_WORDS; i = i + 1) task->frame[i] = 0;
    task->frame[1] = task->arg;
    task->frame[29] = task->stack_top;
    task->frame[32] = 0;
    task->frame[33] = task->entry;
    task->frame[36] = 2;
    task->resume_pc = task->entry;
    task->resume_patch_addr = 0; task->resume_patch_word = 0; task->resume_patch_active = 0;
}


npk_u64 npk_task_switch_trampoline(void)
{
    return NPK_JMP_R15;
}

int npk_task_current_id(npk_u64 *out_id)
{
    struct NpkTaskState *state = npk_task_state();
    if (out_id == 0) return 0;
    *out_id = state->current_task;
    return state->current_task != 0;
}

npk_u64 npk_task_validate(struct NpkOperation *op)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskTcb *task;
    npk_u64 idx;
    if (state->magic != NPK_TASK_STATE_MAGIC || state->version != NPK_TASK_STATE_VERSION)
        return NPK_REASON_TASK_BAD_STATE;
    if (op->type == NPK_OP_TASK_CREATE) {
        if (op->task_id == 0) return NPK_REASON_TASK_BAD_ID;
        if (npk_task_find_index(op->task_id) != NPK_TASK_NO_INDEX) return NPK_REASON_TASK_DUPLICATE;
        if (npk_task_find_free() == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_EXHAUSTED;
        if (op->task_entry == 0 || (op->task_entry & 3) != 0) return NPK_REASON_TASK_BAD_ENTRY;
        if (op->task_stack == 0 || op->task_stack_size < 64) return NPK_REASON_TASK_BAD_STACK;
        if ((op->task_stack & 7) != 0 || ((op->task_stack + op->task_stack_size) & 7) != 0)
            return NPK_REASON_TASK_BAD_STACK;
        return NPK_REASON_NONE;
    }
    if (op->task_id == 0) return NPK_REASON_TASK_BAD_ID;
    idx = npk_task_find_index(op->task_id);
    if (idx == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_NOT_FOUND;
    task = npk_task_tcb(idx);
    if (op->type == NPK_OP_TASK_TERMINATE) {
        if (task->state == NPK_TASK_TERMINATED) return NPK_REASON_TASK_TERMINATED;
        if (task->state == NPK_TASK_FREE) return NPK_REASON_TASK_BAD_STATE;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_YIELD || op->type == NPK_OP_TASK_BLOCK) {
        if (task->state != NPK_TASK_RUNNING) return NPK_REASON_TASK_NOT_RUNNING;
        if (state->current_task != task->id) return NPK_REASON_TASK_NOT_RUNNING;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_WAKE) {
        if (task->state == NPK_TASK_TERMINATED) return NPK_REASON_TASK_TERMINATED;
        if (task->state != NPK_TASK_BLOCKED) return NPK_REASON_TASK_NOT_BLOCKED;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_QUERY) return NPK_REASON_NONE;
    if (op->type == NPK_OP_TASK_DESTROY) {
        if (task->state == NPK_TASK_RUNNING) return NPK_REASON_TASK_BAD_TRANSITION;
        if (task->state == NPK_TASK_READY || task->state == NPK_TASK_BLOCKED)
            return NPK_REASON_TASK_BAD_TRANSITION;
        if (task->state == NPK_TASK_TERMINATED) return NPK_REASON_NONE;
        return NPK_REASON_TASK_BAD_STATE;
    }
    return NPK_REASON_TASK_UNSUPPORTED;
}

npk_u64 npk_task_execute(struct NpkOperation *op)
{
    struct NpkTaskTcb *task;
    npk_u64 idx;
    if (op->type == NPK_OP_TASK_CREATE) {
        op->task_evidence = npk_task_find_free();
        if (op->task_evidence == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_EXHAUSTED;
        op->provisional = op->task_id;
        return NPK_REASON_NONE;
    }
    idx = npk_task_find_index(op->task_id);
    if (idx == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_NOT_FOUND;
    task = npk_task_tcb(idx);
    op->task_evidence = idx;
    op->provisional = task->state;
    return NPK_REASON_NONE;
}


npk_u64 npk_task_commit(struct NpkOperation *op)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskTcb *task;
    npk_u64 idx;
    if (op->type == NPK_OP_TASK_CREATE) {
        idx = npk_task_find_free();
        if (idx == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_EXHAUSTED;
        task = npk_task_tcb(idx);
        task->id = op->task_id;
        task->generation = task->generation + 1;
        task->owner = op->task_parent;
        task->parent = state->current_task;
        task->entry = op->task_entry;
        task->stack_base = op->task_stack;
        task->stack_size = op->task_stack_size;
        task->stack_top = (op->task_stack + op->task_stack_size) & ~((npk_u64)0xF);
        task->arg = op->task_arg;
        task->address_space = op->task_address_space;
        task->exit_status = 0;
        task->flags = 0; /* bit0=resumable trap context captured */
        npk_task_init_frame(task);
        npk_task_set_state(task, NPK_TASK_READY);
        if (npk_task_ready_push(task->id, idx) == 0) {
            task->state = NPK_TASK_FREE;
            task->id = 0;
            npk_task_refresh_counts();
            return NPK_REASON_TASK_EXHAUSTED;
        }
        if (state->next_task_id <= task->id) {
            state->next_task_id = task->id + 1;
        }
        op->result = task->id;
        op->task_evidence = (task->generation << 32) | idx;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    idx = npk_task_find_index(op->task_id);
    if (idx == NPK_TASK_NO_INDEX) return NPK_REASON_TASK_NOT_FOUND;
    task = npk_task_tcb(idx);
    if (op->type == NPK_OP_TASK_TERMINATE) {
        task->exit_status = op->task_arg;
        npk_task_clear_resume_patch(task);
        npk_task_set_state(task, NPK_TASK_TERMINATED);
        if (state->current_task == task->id) {
            state->current_task = 0;
            state->current_index = NPK_TASK_NO_INDEX;
        }
        op->result = task->exit_status;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_YIELD) {
        /* Real schedule decision happens in trap-return hook; commit records intent. */
        op->result = 0;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_BLOCK) {
        /* Blocked transition is applied by scheduler hook for current task. */
        op->result = 0;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_WAKE) {
        /* Reserve the bounded queue slot before changing state so exhaustion is
         * reported without a partial transition. */
        if (npk_task_ready_push(task->id, idx) == 0) return NPK_REASON_TASK_EXHAUSTED;
        npk_task_set_state(task, NPK_TASK_READY);
        op->result = 0;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_QUERY) {
        op->result = task->state;
        state->last_task = task->id;
        return NPK_REASON_NONE;
    }
    if (op->type == NPK_OP_TASK_DESTROY) {
        npk_u64 destroyed_id = task->id;
        npk_task_clear_resume_patch(task);
        task->id = 0;
        task->owner = 0;
        task->parent = 0;
        task->entry = 0;
        task->stack_base = 0;
        task->stack_size = 0;
        task->stack_top = 0;
        task->arg = 0;
        task->address_space = 0;
        task->exit_status = 0;
        task->resume_pc = 0;
        task->flags = 0;
        task->state = NPK_TASK_FREE;
        npk_task_refresh_counts();
        op->result = destroyed_id;
        state->last_task = destroyed_id;
        return NPK_REASON_NONE;
    }
    return NPK_REASON_TASK_UNSUPPORTED;
}

static void npk_task_bootstrap_current(struct NpkTrapFrame *frame)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskTcb *task;
    npk_u64 idx;
    if (state->current_task != 0) return;
    idx = npk_task_find_free();
    if (idx == NPK_TASK_NO_INDEX) return;
    task = npk_task_tcb(idx);
    task->id = state->next_task_id;
    task->generation = 1;
    task->state = NPK_TASK_RUNNING;
    task->owner = 0;
    task->parent = 0;
    task->entry = frame->epc;
    task->stack_base = 0;
    task->stack_size = 0;
    task->stack_top = frame->gpr[29];
    task->arg = 0;
    task->address_space = 0;
    task->exit_status = 0;
    task->resume_pc = frame->epc;
    task->resume_patch_addr = 0;
    task->resume_patch_word = 0;
    task->resume_patch_active = 0;
    task->flags = 1; /* resumable: captured live trap context */
    npk_task_copy_frame_to_tcb(task, frame);
    state->current_task = task->id;
    state->current_index = idx;
    state->next_task_id = state->next_task_id + 1;
    npk_task_refresh_counts();
}


int npk_task_schedule(struct NpkTrapFrame *frame, npk_u64 cause, npk_u64 *out_resume_pc)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskTcb *current;
    struct NpkTaskTcb *next;
    npk_u64 current_idx, next_id, next_idx;
    if (out_resume_pc != 0) *out_resume_pc = 0;
    if (frame == 0 || state->magic != NPK_TASK_STATE_MAGIC) return 0;
    npk_task_bootstrap_current(frame);
    current_idx = state->current_index;
    if (current_idx == NPK_TASK_NO_INDEX || current_idx >= NPK_TASK_SLOTS) return 0;
    current = npk_task_tcb(current_idx);
    if (current->id != state->current_task) return 0;
    state->dispatch_count = state->dispatch_count + 1;

    npk_task_copy_frame_to_tcb(current, frame);
    current->resume_pc = frame->epc;
    current->resume_patch_addr = frame->epc;

    if (cause == NPK_OP_TASK_YIELD) {
        npk_task_set_state(current, NPK_TASK_READY);
        npk_task_ready_push(current->id, current_idx);
    } else if (cause == NPK_OP_TASK_BLOCK) {
        npk_task_set_state(current, NPK_TASK_BLOCKED);
    } else if (cause == NPK_OP_TASK_TERMINATE) {
        current->exit_status = frame->gpr[1];
        npk_task_clear_resume_patch(current);
        npk_task_set_state(current, NPK_TASK_TERMINATED);
    } else {
        npk_task_set_state(current, NPK_TASK_READY);
        npk_task_ready_push(current->id, current_idx);
    }

    /* Only schedule tasks with captured resumable trap context. Freshly created
     * tasks have no live trap frame and cannot be started by patching a syscall
     * return site; they remain READY until a future execution model provides a
     * start path. This keeps cooperative scheduling honest. */
    next_id = npk_task_ready_pop_resumable();
    if (next_id == NPK_TASK_NO_INDEX) {
        /* No runnable resumable task: yield resumes current task; terminated/blocked exits. */
        if (current->state == NPK_TASK_READY) {
            npk_task_clear_resume_patch(current);
            npk_task_set_state(current, NPK_TASK_RUNNING);
            state->current_task = current->id;
            state->current_index = current_idx;
            npk_task_copy_tcb_to_frame(frame, current);
            return 0;
        }
        if (current->state == NPK_TASK_TERMINATED || current->state == NPK_TASK_BLOCKED) {
            npk_u64 halt_pc = npk_task_halt_pc();
            state->current_task = 0;
            state->current_index = NPK_TASK_NO_INDEX;
            npk_task_refresh_counts();
            /* No runnable task remains: patch return site to halt trampoline. */
            if (npk_task_apply_resume_patch(current) != 0) {
                frame->gpr[15] = halt_pc;
                if (out_resume_pc != 0) *out_resume_pc = halt_pc;
                return 1;
            }
            /* Fallback: cannot patch; leave frame unchanged. */
            state->last_reason = NPK_REASON_TASK_BAD_CONTEXT;
            return 0;
        }
        npk_task_set_state(current, NPK_TASK_RUNNING);
        state->current_task = current->id;
        state->current_index = current_idx;
        return 0;
    }
    next_idx = npk_task_find_index(next_id);
    if (next_idx == NPK_TASK_NO_INDEX) return 0;
    next = npk_task_tcb(next_idx);
    npk_task_clear_resume_patch(next);
    npk_task_set_state(next, NPK_TASK_RUNNING);
    state->current_task = next->id;
    state->current_index = next_idx;
    state->last_task = next->id;
    if (next->id != current->id) {
        state->switch_count = state->switch_count + 1;
    }

    if (next->id != current->id) {
        if (npk_task_apply_resume_patch(current) == 0) {
            /* Cannot safely redirect; requeue selected task and keep current running. */
            state->last_reason = NPK_REASON_TASK_BAD_CONTEXT;
            npk_task_set_state(next, NPK_TASK_READY);
            npk_task_ready_push(next->id, next_idx);
            npk_task_set_state(current, NPK_TASK_RUNNING);
            state->current_task = current->id;
            state->current_index = current_idx;
            npk_task_copy_tcb_to_frame(frame, current);
            return 0;
        }
        npk_task_copy_tcb_to_frame(frame, next);
        frame->gpr[15] = next->resume_pc;
        if (out_resume_pc != 0) *out_resume_pc = next->resume_pc;
        return 1;
    }
    npk_task_clear_resume_patch(current);
    npk_task_copy_tcb_to_frame(frame, current);
    if (out_resume_pc != 0) *out_resume_pc = 0;
    return 0;
}

static void npk_task_write_response(npk_u64 status, npk_u64 op, npk_u64 reason,
                                    npk_u64 result, npk_u64 task,
                                    npk_u64 receipt_index, npk_u64 slot)
{
    struct NpkTaskResponse *resp = npk_task_resp();
    resp->magic = NPK_TASK_RESPONSE_MAGIC;
    resp->status = status;
    resp->operation = op;
    resp->reason = reason;
    resp->result = result;
    resp->task = task;
    resp->receipt_index = receipt_index;
    resp->slot = slot;
}

int npk_task_submit_request(void)
{
    struct NpkTaskState *state = npk_task_state();
    struct NpkTaskRequest *req = npk_task_req();
    npk_u64 slot = NPK_TASK_NO_INDEX;
    npk_u64 outcome, reason, result;
    int status;

    if (state->magic != NPK_TASK_STATE_MAGIC) {
        npk_task_write_response(NPK_STATUS_BAD_REQUEST, 0, NPK_REASON_TASK_BAD_STATE, 0, 0, NPK_TASK_NO_INDEX, NPK_TASK_NO_INDEX);
        return NPK_STATUS_BAD_REQUEST;
    }
    if (req->magic != NPK_TASK_REQUEST_MAGIC) {
        npk_task_write_response(NPK_STATUS_BAD_REQUEST, req->operation, NPK_REASON_TASK_BAD_REQUEST, 0, req->task, NPK_TASK_NO_INDEX, NPK_TASK_NO_INDEX);
        return NPK_STATUS_BAD_REQUEST;
    }
    state->dispatch_count = state->dispatch_count + 1;
    status = npk_core_admit(req->operation, req->id, req->task, &slot);
    if (status != 0) {
        reason = NPK_REASON_TASK_EXHAUSTED;
        state->last_status = NPK_STATUS_ADMIT_FAIL;
        state->last_reason = reason;
        npk_task_write_response(NPK_STATUS_ADMIT_FAIL, req->operation, reason, 0, req->task, NPK_TASK_NO_INDEX, NPK_TASK_NO_INDEX);
        return NPK_STATUS_ADMIT_FAIL;
    }
    npk_core_set_task_arguments(slot, req->task, req->arg0, req->arg1, req->arg2, req->arg3, req->arg4, req->arg5);

    for (;;) {
        outcome = npk_core_operation_outcome(slot);
        if (outcome != NPK_OUTCOME_PENDING) break;
        status = npk_core_advance(slot);
        if (status != 0) {
            state->last_status = NPK_STATUS_ADVANCE_FAIL;
            npk_task_write_response(NPK_STATUS_ADVANCE_FAIL, req->operation, NPK_REASON_TASK_BAD_STATE, 0, req->task, npk_core_operation_receipt_index(slot), slot);
            npk_core_release(slot);
            return NPK_STATUS_ADVANCE_FAIL;
        }
    }
    reason = npk_core_operation_reason(slot);
    result = npk_core_operation_result(slot);
    state->last_status = outcome == NPK_OUTCOME_COMPLETE ? NPK_STATUS_OK : NPK_STATUS_BAD_OUTCOME;
    state->last_reason = reason;
    state->last_task = req->task;
    npk_task_write_response(state->last_status, req->operation, reason, result, req->task,
                            npk_core_operation_receipt_index(slot), slot);
    npk_core_release(slot);
    return (int)state->last_status;
}

static void npk_task_ready_remove(npk_u64 task_id)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 i;
    npk_u64 count = state->ready_count;

    for (i = 0; i < count; i = i + 1) {
        if (npk_task_ready(i)->task_id != task_id) {
            continue;
        }
        for (; i + 1 < count; i = i + 1) {
            *npk_task_ready(i) = *npk_task_ready(i + 1);
        }
        state->ready_count = count - 1;
        npk_task_ready(state->ready_count)->task_id = 0;
        npk_task_ready(state->ready_count)->task_index = NPK_TASK_NO_INDEX;
        return;
    }
}

npk_u64 npk_task_can_bind_execution(npk_u64 task_id, npk_u64 owner)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 index = npk_task_find_live_index(task_id);
    struct NpkTaskTcb *task;

    if (index == NPK_TASK_NO_INDEX) {
        return NPK_REASON_TASK_NOT_FOUND;
    }
    task = npk_task_tcb(index);
    if (task->owner != owner) {
        return NPK_REASON_TASK_BAD_ID;
    }
    if (task->state != NPK_TASK_CREATED && task->state != NPK_TASK_READY) {
        return NPK_REASON_TASK_BAD_TRANSITION;
    }
    if (state->current_task == task_id) {
        return NPK_REASON_TASK_BAD_STATE;
    }
    return NPK_REASON_NONE;
}

npk_u64 npk_task_bind_execution(npk_u64 task_id, npk_u64 owner,
                                npk_u64 entry, npk_u64 stack_base,
                                npk_u64 stack_size, npk_u64 arg,
                                npk_u64 address_space)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 index = npk_task_find_live_index(task_id);
    struct NpkTaskTcb *task;
    npk_u64 reason = npk_task_can_bind_execution(task_id, owner);

    if (reason != NPK_REASON_NONE) {
        return reason;
    }
    task = npk_task_tcb(index);
    npk_task_ready_remove(task_id);
    task->entry = entry;
    task->stack_base = stack_base;
    task->stack_size = stack_size;
    task->stack_top = stack_base + stack_size;
    task->arg = arg;
    task->address_space = address_space;
    task->exit_status = 0;
    task->resume_pc = entry;
    task->resume_patch_addr = 0;
    task->resume_patch_word = 0;
    task->resume_patch_active = 0;
    task->frame[0] = 0;
    task->frame[1] = arg;
    task->frame[29] = task->stack_top;
    task->frame[31] = 0;
    task->frame[32] = 0;
    task->frame[33] = entry;
    task->frame[34] = 0;
    task->frame[35] = 0;
    task->frame[36] = 0;
    task->frame[37] = 0;
    /* Bit 1 records a VM-entry execution context. It is deliberately distinct
     * from bit 0, which marks an MRET-resumable trap context in the cooperative
     * scheduler. */
    task->flags = 2;
    state->current_task = task_id;
    state->current_index = index;
    npk_task_set_state(task, NPK_TASK_RUNNING);
    return NPK_REASON_NONE;
}

npk_u64 npk_task_finish_execution(npk_u64 task_id, npk_u64 exit_code)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 index = npk_task_find_live_index(task_id);
    struct NpkTaskTcb *task;

    if (index == NPK_TASK_NO_INDEX) {
        return NPK_REASON_TASK_NOT_FOUND;
    }
    task = npk_task_tcb(index);
    if (task->state != NPK_TASK_RUNNING) {
        return NPK_REASON_TASK_BAD_TRANSITION;
    }
    task->exit_status = exit_code;
    if (state->current_task == task_id) {
        state->current_task = 0;
        state->current_index = NPK_TASK_NO_INDEX;
    }
    npk_task_set_state(task, NPK_TASK_TERMINATED);
    return NPK_REASON_NONE;
}

int npk_task_init(void)
{
    struct NpkTaskState *state = npk_task_state();
    npk_u64 i;
    for (i = 0; i < sizeof(struct NpkTaskState) / 8; i = i + 1)
        ((npk_u64 *)state)[i] = 0;
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1) {
        struct NpkTaskTcb *task = npk_task_tcb(i);
        npk_u64 j;
        for (j = 0; j < NPK_TASK_TCB_SIZE / 8; j = j + 1) ((npk_u64 *)task)[j] = 0;
        task->state = NPK_TASK_FREE;
        task->resume_patch_addr = 0;
    }
    for (i = 0; i < NPK_TASK_SLOTS; i = i + 1) {
        npk_task_ready(i)->task_id = 0;
        npk_task_ready(i)->task_index = NPK_TASK_NO_INDEX;
    }
    state->magic = NPK_TASK_STATE_MAGIC;
    state->version = NPK_TASK_STATE_VERSION;
    state->init_status = 0;
    state->task_slots = NPK_TASK_SLOTS;
    state->current_index = NPK_TASK_NO_INDEX;
    state->next_task_id = NPK_TASK_ID_BASE | 1;
    return 0;
}
