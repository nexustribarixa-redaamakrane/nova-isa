/*
 * NPK Milestone 6 syscall and privilege-context processing.
 *
 * Calls enter through the real Milestone 3 SCALL trap path. Every call becomes
 * a typed operation on the shared processing core and traverses
 * ADMIT -> VALIDATE -> EXECUTE -> COMMIT -> COMPLETE with its own bounded
 * receipt log. Arguments are read from the saved trap frame and ABI results are
 * written back to that frame only after commit, or after a terminal failure,
 * before trap.s restores registers and executes MRET.
 *
 * Nova ISA v1.0 does not expose the pre-trap privilege in the software trap
 * frame; the recorded handler privilege is always machine mode. The subsystem
 * validates that machine-mode handler context, but it cannot claim caller
 * privilege enforcement. There is no process model or scheduler here. SYS_EXIT
 * and SYS_YIELD record policy requests without terminating or scheduling
 * anything, and SYS_WRITE publishes to the bounded console sink owned by the
 * Milestone 7 device backend.
 */

#include "npk_core.h"

#define NPK_TRAP_FRAME_ADDRESS 0x90900

#define NPK_SYSCALL_STATE_ADDRESS 0x9FA10
#define NPK_SYSCALL_WRITE_LOG_ADDRESS 0x9FE90
#define NPK_SYSCALL_SCRATCH_ADDRESS 0x9FF90

#define NPK_SYSCALL_STATE_MAGIC 0x4E504B53
#define NPK_SYSCALL_STATE_VERSION 1

#define NPK_OP_SYSCALL_SERVICE 21

#define NPK_REASON_NONE 0
#define NPK_REASON_SYSCALL_BAD_FRAME 46
#define NPK_REASON_SYSCALL_BAD_STATE 47
#define NPK_REASON_SYSCALL_BAD_NUMBER 48
#define NPK_REASON_SYSCALL_BAD_ARGUMENT 49
#define NPK_REASON_SYSCALL_BAD_PRIVILEGE 50
#define NPK_REASON_SYSCALL_BAD_POINTER 51
#define NPK_REASON_SYSCALL_UNSUPPORTED 52
#define NPK_REASON_SYSCALL_NO_RESOURCE 53
#define NPK_REASON_SYSCALL_NOT_FOUND 54
#define NPK_REASON_SYSCALL_COMMIT 55
#define NPK_REASON_SYSCALL_CHILD 56

#define NPK_OUTCOME_NONE 0
#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_STATUS_OK 0
#define NPK_STATUS_BAD_STATE 1
#define NPK_STATUS_BAD_FRAME 2
#define NPK_STATUS_ADMIT_FAIL 3
#define NPK_STATUS_ADVANCE_FAIL 4
#define NPK_STATUS_BAD_OUTCOME 5

#define NPK_ERROR_NONE 0
#define NPK_ERROR_BAD_ARGUMENT 1
#define NPK_ERROR_UNSUPPORTED 2
#define NPK_ERROR_NOT_FOUND 3
#define NPK_ERROR_NO_RESOURCE 4

#define NPK_SYS_EXIT 0
#define NPK_SYS_WRITE 1
#define NPK_SYS_READ 2
#define NPK_SYS_TIME 3
#define NPK_SYS_OBJ_CREATE 4
#define NPK_SYS_OBJ_SIZE 5
#define NPK_SYS_OBJ_DESTROY 6
#define NPK_SYS_YIELD 7
#define NPK_SYSCALL_COUNT 8

#define NPK_SYSCALL_WRITE_LOG_SIZE 256
#define NPK_SYSCALL_SCRATCH_SIZE 64
#define NPK_SYSCALL_MAX_WRITE 64
#define NPK_SYSCALL_STAGE_MAGIC 0x4E504B5354414745

#define NPK_ACCESS_READ 1
#define NPK_MEMORY_REASON_NONE 0

#define NPK_REQUEST_OK 0
#define NPK_REQUEST_BAD_REQUEST 1
#define NPK_REQUEST_BAD_OP 2
#define NPK_REQUEST_ADMIT_FAIL 3
#define NPK_REQUEST_ADVANCE_FAIL 4
#define NPK_REQUEST_BAD_OUTCOME 5
#define NPK_REQUEST_REJECTED 6
#define NPK_REQUEST_FAILED 7

#define NPK_REASON_OBJECT_BAD_REQUEST 30
#define NPK_REASON_OBJECT_BAD_TYPE 31
#define NPK_REASON_OBJECT_BAD_HANDLE 32
#define NPK_REASON_OBJECT_BAD_OBJECT 33
#define NPK_REASON_OBJECT_BAD_OWNER 34
#define NPK_REASON_OBJECT_BAD_PERMISSION 35
#define NPK_REASON_OBJECT_STATE 36
#define NPK_REASON_OBJECT_EXHAUSTED 37
#define NPK_REASON_OBJECT_DUPLICATE 38
#define NPK_REASON_OBJECT_BAD_PARENT 39
#define NPK_REASON_OBJECT_PAYLOAD 40
#define NPK_REASON_OBJECT_UNSUPPORTED 41
#define NPK_REASON_OBJECT_BAD_NAME 42
#define NPK_REASON_OBJECT_NO_ENTRY 43
#define NPK_REASON_OBJECT_NOT_DIRECTORY 44
#define NPK_REASON_OBJECT_BAD_PATH 45

#define NPK_OP_OBJECT_CREATE 9
#define NPK_OP_OBJECT_DESTROY 10
#define NPK_OP_OBJECT_CLOSE 12
#define NPK_OP_OBJECT_QUERY 13

#define NPK_OBJECT_TYPE_GENERIC 1
#define NPK_OBJ_PERM_ALL 0x1F
#define NPK_SYSCALL_OWNER 1

#define NPK_SYSCALL_ID_BASE 0x53590000
#define NPK_OBJECT_REQUEST_ID_BASE 0x4F420000

#define NPK_FLAG_HANDLER_CONTEXT 1
#define NPK_FLAG_EXIT_REQUESTED 2
#define NPK_FLAG_YIELD_REQUESTED 4

#define NPK_U64_MAX 0xFFFFFFFFFFFFFFFF
#define NPK_SYSCALL_FAILURE 0xFFFFFFFFFFFFFFFF

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);
extern npk_u64 npk_core_operation_syscall_error(npk_u64 slot);
extern npk_u64 npk_core_operation_syscall_child_receipt(npk_u64 slot);
extern void npk_core_set_syscall_arguments(npk_u64 slot, npk_u64 number,
                                           npk_u64 error, npk_u64 *args);
extern npk_u64 npk_object_handle_for_id(npk_u64 object_id, npk_u64 owner);
extern npk_u64 npk_object_id_for_handle(npk_u64 handle, npk_u64 owner);
extern npk_u64 npk_object_payload_for_handle(npk_u64 handle, npk_u64 owner);
extern int npk_object_syscall_request(struct NpkObjectSyscallRequest *request,
                                      npk_u64 *out_result, npk_u64 *out_reason,
                                      npk_u64 *out_receipt);
extern npk_u64 npk_memory_user_physical(npk_u64 address, npk_u64 access,
                                        npk_u64 *out_physical);
extern npk_u64 npk_syscall_read_time(void);

struct NpkSyscallState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 dispatch_count;
    npk_u64 last_number;
    npk_u64 last_arg_count;
    npk_u64 last_op_slot;
    npk_u64 last_status;
    npk_u64 last_outcome;
    npk_u64 last_error;
    npk_u64 last_result;
    npk_u64 last_receipt_index;
    npk_u64 last_reason;
    npk_u64 child_receipt;
    npk_u64 flags;
    npk_u64 write_log_count;
};

static struct NpkSyscallState *npk_state(void)
{
    return (struct NpkSyscallState *)NPK_SYSCALL_STATE_ADDRESS;
}

static npk_u64 *npk_write_log(void)
{
    return (npk_u64 *)NPK_SYSCALL_WRITE_LOG_ADDRESS;
}

static npk_u64 *npk_scratch(void)
{
    return (npk_u64 *)NPK_SYSCALL_SCRATCH_ADDRESS;
}

static npk_u64 npk_arg_count(npk_u64 number)
{
    if (number == NPK_SYS_EXIT || number == NPK_SYS_OBJ_CREATE ||
            number == NPK_SYS_OBJ_SIZE || number == NPK_SYS_OBJ_DESTROY) {
        return 1;
    }
    if (number == NPK_SYS_WRITE || number == NPK_SYS_READ) {
        return 3;
    }
    if (number == NPK_SYS_TIME || number == NPK_SYS_YIELD) {
        return 0;
    }
    return 0xFF;
}

static npk_u64 npk_m5_error_for_reason(npk_u64 reason)
{
    if (reason == NPK_REASON_OBJECT_BAD_HANDLE ||
            reason == NPK_REASON_OBJECT_BAD_OBJECT ||
            reason == NPK_REASON_OBJECT_NO_ENTRY ||
            reason == NPK_REASON_OBJECT_STATE) {
        return NPK_ERROR_NOT_FOUND;
    }
    if (reason == NPK_REASON_OBJECT_EXHAUSTED) {
        return NPK_ERROR_NO_RESOURCE;
    }
    return NPK_ERROR_BAD_ARGUMENT;
}

static npk_u64 npk_error_for_reason(npk_u64 reason)
{
    if (reason == NPK_REASON_SYSCALL_BAD_NUMBER ||
            reason == NPK_REASON_SYSCALL_UNSUPPORTED) {
        return NPK_ERROR_UNSUPPORTED;
    }
    if (reason == NPK_REASON_SYSCALL_NO_RESOURCE ||
            reason == NPK_REASON_SYSCALL_BAD_STATE) {
        return NPK_ERROR_NO_RESOURCE;
    }
    if (reason == NPK_REASON_SYSCALL_NOT_FOUND ||
            reason == NPK_REASON_SYSCALL_CHILD) {
        return NPK_ERROR_NOT_FOUND;
    }
    if (reason >= NPK_REASON_OBJECT_BAD_REQUEST &&
            reason <= NPK_REASON_OBJECT_BAD_PATH) {
        return npk_m5_error_for_reason(reason);
    }
    return NPK_ERROR_BAD_ARGUMENT;
}

static npk_u64 npk_error_for_child(npk_u64 status, npk_u64 reason)
{
    if (status == NPK_REQUEST_ADMIT_FAIL ||
            status == NPK_REQUEST_ADVANCE_FAIL ||
            status == NPK_REQUEST_BAD_OUTCOME) {
        return NPK_ERROR_NO_RESOURCE;
    }
    if (status != NPK_REQUEST_OK) {
        if (reason == NPK_REASON_NONE) {
            return NPK_ERROR_BAD_ARGUMENT;
        }
        return npk_m5_error_for_reason(reason);
    }
    return NPK_ERROR_NONE;
}

static npk_u64 npk_load_byte(npk_u64 address)
{
    npk_u64 aligned = address & ~(npk_u64)7;
    npk_u64 shift = (address & 7) * 8;
    npk_u64 word = *((npk_u64 *)aligned);

    return (word >> shift) & 0xFF;
}

static void npk_store_byte(npk_u64 address, npk_u64 value)
{
    npk_u64 aligned = address & ~(npk_u64)7;
    npk_u64 shift = (address & 7) * 8;
    npk_u64 mask = 0xFF << shift;
    npk_u64 word = *((npk_u64 *)aligned);

    word = (word & ~mask) | ((value & 0xFF) << shift);
    *((npk_u64 *)aligned) = word;
}

static npk_u64 npk_user_byte(npk_u64 address, npk_u64 *out_byte)
{
    npk_u64 physical = 0;
    npk_u64 reason;

    reason = npk_memory_user_physical(address, NPK_ACCESS_READ, &physical);
    if (reason != NPK_MEMORY_REASON_NONE) {
        return reason;
    }
    *out_byte = npk_load_byte(physical);
    return NPK_MEMORY_REASON_NONE;
}

static void npk_publish_error(struct NpkTrapFrame *frame, npk_u64 error)
{
    /* gPR 0 is the hardwired zero register; a0/a1 are GPRs 1/2. */
    frame->gpr[1] = NPK_SYSCALL_FAILURE;
    frame->gpr[2] = error;
}

int npk_syscall_init(void)
{
    struct NpkSyscallState *state = npk_state();
    npk_u64 i;

    for (i = 0; i < 16; i = i + 1) {
        *((npk_u64 *)state + i) = 0;
    }
    for (i = 0; i < NPK_SYSCALL_WRITE_LOG_SIZE / 8; i = i + 1) {
        npk_write_log()[i] = 0;
    }
    for (i = 0; i < NPK_SYSCALL_SCRATCH_SIZE / 8; i = i + 1) {
        npk_scratch()[i] = 0;
    }
    state->magic = NPK_SYSCALL_STATE_MAGIC;
    state->version = NPK_SYSCALL_STATE_VERSION;
    state->init_status = 0;
    state->last_op_slot = NPK_NO_INDEX;
    state->last_receipt_index = NPK_NO_INDEX;
    state->child_receipt = NPK_NO_INDEX;
    return NPK_STATUS_OK;
}

npk_u64 npk_syscall_write_log_count(void)
{
    return npk_state()->write_log_count;
}

void npk_syscall_write_log_advance(npk_u64 count)
{
    struct NpkSyscallState *state = npk_state();

    state->write_log_count = state->write_log_count + count;
}

npk_u64 npk_syscall_validate(struct NpkOperation *op)
{
    struct NpkTrapFrame *frame = (struct NpkTrapFrame *)op->input;
    struct NpkSyscallState *state = npk_state();
    npk_u64 count;
    npk_u64 byte;
    npk_u64 i;

    if (op->input != NPK_TRAP_FRAME_ADDRESS || frame->ecode != 7 ||
            frame->epc % 4 != 0) {
        op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_SYSCALL_BAD_FRAME;
    }
    if (state->magic != NPK_SYSCALL_STATE_MAGIC ||
            state->version != NPK_SYSCALL_STATE_VERSION ||
            state->init_status != 0) {
        op->syscall_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_SYSCALL_BAD_STATE;
    }
    if ((state->flags & NPK_FLAG_HANDLER_CONTEXT) == 0 ||
            frame->handler_priv != 2) {
        op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_SYSCALL_BAD_PRIVILEGE;
    }
    if (op->syscall_number >= NPK_SYSCALL_COUNT) {
        op->syscall_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_SYSCALL_BAD_NUMBER;
    }
    count = npk_arg_count(op->syscall_number);
    if (count == 0xFF) {
        op->syscall_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_SYSCALL_BAD_NUMBER;
    }
    if (op->syscall_number == NPK_SYS_READ) {
        op->syscall_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_SYSCALL_UNSUPPORTED;
    }
    if (op->syscall_number == NPK_SYS_OBJ_SIZE ||
            op->syscall_number == NPK_SYS_OBJ_DESTROY) {
        if (op->syscall_arg[0] == 0 ||
                npk_object_id_for_handle(op->syscall_arg[0],
                                         NPK_SYSCALL_OWNER) == NPK_NO_INDEX) {
            op->syscall_error = NPK_ERROR_NOT_FOUND;
            return NPK_REASON_SYSCALL_NOT_FOUND;
        }
    }
    if (op->syscall_number != NPK_SYS_WRITE) {
        return NPK_REASON_NONE;
    }
    if (op->syscall_arg[0] > 2 ||
            op->syscall_arg[2] > NPK_SYSCALL_MAX_WRITE) {
        op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_SYSCALL_BAD_ARGUMENT;
    }
    if (state->write_log_count + op->syscall_arg[2] >
            NPK_SYSCALL_WRITE_LOG_SIZE) {
        op->syscall_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_SYSCALL_NO_RESOURCE;
    }
    if (op->syscall_arg[2] == 0) {
        return NPK_REASON_NONE;
    }
    if (op->syscall_arg[1] > NPK_U64_MAX - (op->syscall_arg[2] - 1)) {
        op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
        return NPK_REASON_SYSCALL_BAD_POINTER;
    }
    for (i = 0; i < op->syscall_arg[2]; i = i + 1) {
        if (npk_user_byte(op->syscall_arg[1] + i, &byte) !=
                NPK_MEMORY_REASON_NONE) {
            op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
            return NPK_REASON_SYSCALL_BAD_POINTER;
        }
    }
    return NPK_REASON_NONE;
}

static void npk_object_request_init(struct NpkObjectSyscallRequest *request,
                                    npk_u64 operation, npk_u64 id)
{
    npk_u64 i;

    request->operation = operation;
    request->id = id;
    request->owner = NPK_SYSCALL_OWNER;
    request->type = 0;
    request->handle = 0;
    request->permissions = 0;
    request->payload = 0;
    request->parent = 0;
    request->aux = 0;
    for (i = 0; i < 4; i = i + 1) {
        request->name[i] = 0;
    }
}

static npk_u64 npk_child_reason(struct NpkOperation *op, npk_u64 status,
                                npk_u64 reason, npk_u64 receipt)
{
    op->syscall_child_receipt = receipt;
    op->syscall_error = npk_error_for_child(status, reason);
    if (op->syscall_error == NPK_ERROR_NONE) {
        op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
    }
    return NPK_REASON_SYSCALL_CHILD;
}

static npk_u64 npk_object_create(struct NpkOperation *op, npk_u64 id)
{
    struct NpkObjectSyscallRequest request;
    npk_u64 result = 0;
    npk_u64 reason = 0;
    npk_u64 receipt = NPK_NO_INDEX;
    npk_u64 handle;
    int status;

    npk_object_request_init(&request, NPK_OP_OBJECT_CREATE, id);
    request.type = NPK_OBJECT_TYPE_GENERIC;
    request.permissions = NPK_OBJ_PERM_ALL;
    request.payload = op->syscall_arg[0];
    status = npk_object_syscall_request(&request, &result, &reason, &receipt);
    op->syscall_child_receipt = receipt;
    if (status != NPK_REQUEST_OK) {
        return npk_child_reason(op, status, reason, receipt);
    }
    handle = npk_object_handle_for_id(result, NPK_SYSCALL_OWNER);
    if (handle == NPK_NO_INDEX) {
        op->syscall_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_SYSCALL_CHILD;
    }
    op->provisional = handle;
    op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
    return NPK_REASON_NONE;
}

static npk_u64 npk_object_size(struct NpkOperation *op, npk_u64 id)
{
    struct NpkObjectSyscallRequest request;
    npk_u64 result = 0;
    npk_u64 reason = 0;
    npk_u64 receipt = NPK_NO_INDEX;
    npk_u64 payload;
    int status;

    npk_object_request_init(&request, NPK_OP_OBJECT_QUERY, id);
    request.handle = op->syscall_arg[0];
    status = npk_object_syscall_request(&request, &result, &reason, &receipt);
    op->syscall_child_receipt = receipt;
    if (status != NPK_REQUEST_OK) {
        return npk_child_reason(op, status, reason, receipt);
    }
    payload = npk_object_payload_for_handle(op->syscall_arg[0],
                                            NPK_SYSCALL_OWNER);
    if (payload == NPK_NO_INDEX) {
        op->syscall_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_SYSCALL_CHILD;
    }
    op->provisional = payload;
    op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
    return NPK_REASON_NONE;
}

static npk_u64 npk_object_destroy(struct NpkOperation *op, npk_u64 id)
{
    struct NpkObjectSyscallRequest request;
    npk_u64 result = 0;
    npk_u64 reason = 0;
    npk_u64 close_receipt = NPK_NO_INDEX;
    npk_u64 destroy_receipt = NPK_NO_INDEX;
    npk_u64 object_id;
    int status;

    object_id = npk_object_id_for_handle(op->syscall_arg[0],
                                         NPK_SYSCALL_OWNER);
    if (object_id == NPK_NO_INDEX) {
        op->syscall_error = NPK_ERROR_NOT_FOUND;
        return NPK_REASON_SYSCALL_NOT_FOUND;
    }

    npk_object_request_init(&request, NPK_OP_OBJECT_CLOSE, id);
    request.handle = op->syscall_arg[0];
    status = npk_object_syscall_request(&request, &result, &reason,
                                        &close_receipt);
    if (status != NPK_REQUEST_OK) {
        return npk_child_reason(op, status, reason, close_receipt);
    }

    npk_object_request_init(&request, NPK_OP_OBJECT_DESTROY, id + 1);
    request.payload = object_id;
    status = npk_object_syscall_request(&request, &result, &reason,
                                        &destroy_receipt);
    op->syscall_child_receipt = (close_receipt << 32) | destroy_receipt;
    if (status != NPK_REQUEST_OK) {
        return npk_child_reason(op, status, reason, destroy_receipt);
    }
    op->provisional = 0;
    op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
    return NPK_REASON_NONE;
}

npk_u64 npk_syscall_execute(struct NpkOperation *op)
{
    struct NpkSyscallState *state = npk_state();
    npk_u64 child_id;
    npk_u64 address;
    npk_u64 count;
    npk_u64 byte;
    npk_u64 i;

    if (op->syscall_number == NPK_SYS_OBJ_CREATE ||
            op->syscall_number == NPK_SYS_OBJ_SIZE ||
            op->syscall_number == NPK_SYS_OBJ_DESTROY) {
        child_id = NPK_OBJECT_REQUEST_ID_BASE + state->dispatch_count * 4;
        if (op->syscall_number == NPK_SYS_OBJ_CREATE) {
            op->provisional = 0;
            return npk_object_create(op, child_id);
        }
        if (op->syscall_number == NPK_SYS_OBJ_SIZE) {
            op->provisional = 0;
            return npk_object_size(op, child_id);
        }
        op->provisional = 0;
        return npk_object_destroy(op, child_id);
    }
    if (op->syscall_number == NPK_SYS_EXIT ||
            op->syscall_number == NPK_SYS_YIELD) {
        op->provisional = 0;
        op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
        return NPK_REASON_NONE;
    }
    if (op->syscall_number == NPK_SYS_TIME) {
        op->provisional = npk_syscall_read_time();
        op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
        return NPK_REASON_NONE;
    }
    if (op->syscall_number != NPK_SYS_WRITE) {
        op->syscall_error = NPK_ERROR_UNSUPPORTED;
        return NPK_REASON_SYSCALL_UNSUPPORTED;
    }

    count = op->syscall_arg[2];
    address = op->syscall_arg[1];
    if (state->write_log_count + count > NPK_SYSCALL_WRITE_LOG_SIZE) {
        op->syscall_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_SYSCALL_NO_RESOURCE;
    }
    for (i = 0; i < count; i = i + 1) {
        if (npk_user_byte(address + i, &byte) != NPK_MEMORY_REASON_NONE) {
            op->syscall_error = NPK_ERROR_BAD_ARGUMENT;
            return NPK_REASON_SYSCALL_BAD_POINTER;
        }
        npk_store_byte(NPK_SYSCALL_SCRATCH_ADDRESS + i, byte);
    }
    op->provisional = count;
    op->memory_scratch = NPK_SYSCALL_STAGE_MAGIC;
    return NPK_REASON_NONE;
}

npk_u64 npk_syscall_commit(struct NpkOperation *op)
{
    struct NpkTrapFrame *frame = (struct NpkTrapFrame *)op->input;
    struct NpkSyscallState *state = npk_state();
    npk_u64 count;
    npk_u64 i;

    if (op->memory_scratch != NPK_SYSCALL_STAGE_MAGIC) {
        op->syscall_error = NPK_ERROR_NO_RESOURCE;
        return NPK_REASON_SYSCALL_COMMIT;
    }
    if (op->syscall_number == NPK_SYS_WRITE) {
        count = op->syscall_arg[2];
        if (state->write_log_count + count > NPK_SYSCALL_WRITE_LOG_SIZE) {
            op->syscall_error = NPK_ERROR_NO_RESOURCE;
            return NPK_REASON_SYSCALL_NO_RESOURCE;
        }
        if (npk_device_console_commit(NPK_SYSCALL_SCRATCH_ADDRESS,
                                      NPK_SYSCALL_WRITE_LOG_ADDRESS,
                                      state->write_log_count, count) != 0) {
            op->syscall_error = NPK_ERROR_NO_RESOURCE;
            return NPK_REASON_SYSCALL_COMMIT;
        }
        state->write_log_count = state->write_log_count + count;
    } else if (op->syscall_number == NPK_SYS_EXIT) {
        state->flags = state->flags | NPK_FLAG_EXIT_REQUESTED;
    } else if (op->syscall_number == NPK_SYS_YIELD) {
        state->flags = state->flags | NPK_FLAG_YIELD_REQUESTED;
    }

    /* Scratch is an uncommitted staging area; it must not retain payload. */
    for (i = 0; i < NPK_SYSCALL_SCRATCH_SIZE / 8; i = i + 1) {
        npk_scratch()[i] = 0;
    }

    op->result = op->provisional;
    op->syscall_error = NPK_ERROR_NONE;
    frame->gpr[1] = op->result;
    frame->gpr[2] = NPK_ERROR_NONE;
    state->last_result = op->result;
    state->last_error = NPK_ERROR_NONE;
    return NPK_REASON_NONE;
}

int npk_syscall_dispatch(struct NpkTrapFrame *frame)
{
    struct NpkSyscallState *state = npk_state();
    npk_u64 args[6];
    npk_u64 number;
    npk_u64 count;
    npk_u64 id;
    npk_u64 slot = NPK_NO_INDEX;
    npk_u64 outcome;
    npk_u64 result;
    npk_u64 error;
    int status = NPK_STATUS_OK;
    int i;

    if (frame == 0 || state->magic != NPK_SYSCALL_STATE_MAGIC ||
            state->version != NPK_SYSCALL_STATE_VERSION) {
        state->last_status = NPK_STATUS_BAD_STATE;
        if (frame != 0) {
            npk_publish_error(frame, NPK_ERROR_NO_RESOURCE);
        }
        return NPK_STATUS_BAD_STATE;
    }

    state->dispatch_count = state->dispatch_count + 1;
    number = frame->ecause;
    count = npk_arg_count(number);
    state->last_number = number;
    state->last_arg_count = count;
    state->last_op_slot = NPK_NO_INDEX;
    state->last_status = NPK_STATUS_OK;
    state->last_outcome = NPK_OUTCOME_NONE;
    state->last_error = NPK_ERROR_NONE;
    state->last_result = 0;
    state->last_receipt_index = NPK_NO_INDEX;
    state->last_reason = NPK_REASON_NONE;
    state->child_receipt = NPK_NO_INDEX;
    state->flags = state->flags & ~NPK_FLAG_HANDLER_CONTEXT;
    if (frame->handler_priv == 2) {
        state->flags = state->flags | NPK_FLAG_HANDLER_CONTEXT;
    }

    for (i = 0; i < 6; i = i + 1) {
        args[i] = frame->gpr[i + 1];
    }
    id = NPK_SYSCALL_ID_BASE + state->dispatch_count;
    status = npk_core_admit(NPK_OP_SYSCALL_SERVICE, id,
                            NPK_TRAP_FRAME_ADDRESS, &slot);
    if (status != 0) {
        state->last_status = NPK_STATUS_ADMIT_FAIL;
        state->last_outcome = NPK_OUTCOME_REJECTED;
        state->last_result = NPK_SYSCALL_FAILURE;
        state->last_error = NPK_ERROR_NO_RESOURCE;
        state->last_reason = NPK_REASON_SYSCALL_NO_RESOURCE;
        state->last_receipt_index = NPK_NO_INDEX;
        npk_publish_error(frame, NPK_ERROR_NO_RESOURCE);
        return NPK_STATUS_ADMIT_FAIL;
    }
    npk_core_set_syscall_arguments(slot, number, NPK_ERROR_NONE, args);

    outcome = NPK_OUTCOME_PENDING;
    for (i = 0; i < 5; i = i + 1) {
        outcome = npk_core_operation_outcome(slot);
        if (outcome != NPK_OUTCOME_PENDING) {
            break;
        }
        status = npk_core_advance(slot);
        if (status != 0) {
            state->last_status = NPK_STATUS_ADVANCE_FAIL;
            break;
        }
    }

    result = npk_core_operation_result(slot);
    error = npk_core_operation_syscall_error(slot);
    state->last_op_slot = slot;
    state->last_outcome = outcome;
    state->last_reason = npk_core_operation_reason(slot);
    state->last_receipt_index = npk_core_operation_receipt_index(slot);
    state->child_receipt = npk_core_operation_syscall_child_receipt(slot);

    if (status == 0) {
        if (outcome == NPK_OUTCOME_COMPLETE) {
            state->last_status = NPK_STATUS_OK;
        } else if (outcome == NPK_OUTCOME_REJECTED ||
                outcome == NPK_OUTCOME_FAILED) {
            state->last_status = NPK_STATUS_OK;
        } else {
            status = NPK_STATUS_BAD_OUTCOME;
            state->last_status = NPK_STATUS_BAD_OUTCOME;
        }
    }
    if (outcome == NPK_OUTCOME_COMPLETE) {
        state->last_result = result;
        state->last_error = NPK_ERROR_NONE;
    } else {
        if (error == NPK_ERROR_NONE) {
            error = npk_error_for_reason(state->last_reason);
        }
        state->last_result = NPK_SYSCALL_FAILURE;
        state->last_error = error;
        npk_publish_error(frame, error);
    }
    npk_core_release(slot);
    return status;
}
