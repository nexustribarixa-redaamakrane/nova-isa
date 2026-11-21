#ifndef NPK_CORE_H
#define NPK_CORE_H

typedef unsigned long npk_u64;

#define NPK_NO_INDEX 0xFFFFFFFFFFFFFFFF

/* Milestone 3 trap frame shared by trap.c and the Milestone 6 syscall service. */
struct NpkTrapFrame {
    npk_u64 gpr[32];
    npk_u64 psr;
    npk_u64 epc;
    npk_u64 ecause;
    npk_u64 ecode;
    npk_u64 handler_priv;
    npk_u64 tval;
};

/* Milestone 6 syscall entry points used by the coordinator and trap dispatcher. */
int npk_syscall_init(void);
int npk_syscall_dispatch(struct NpkTrapFrame *frame);

/* Shared with the trap handlers; the core owns all writes to these fields. */
struct NpkOperation {
    npk_u64 id;
    npk_u64 type;
    npk_u64 stage;
    npk_u64 state;
    npk_u64 pu;
    npk_u64 input;
    npk_u64 provisional;
    npk_u64 result;
    npk_u64 reason;
    npk_u64 outcome;
    npk_u64 receipt_index;
    npk_u64 receipt_base;
    /* Milestone 4 memory arguments and committed-evidence helpers. */
    npk_u64 memory_address;
    npk_u64 memory_size;
    npk_u64 memory_flags;
    npk_u64 memory_aux;
    npk_u64 memory_evidence;
    npk_u64 memory_scratch;
    /* Milestone 5 object-manager arguments and committed-evidence helpers. */
    npk_u64 object_type;
    npk_u64 object_flags;
    npk_u64 object_parent;
    npk_u64 object_payload;
    npk_u64 object_evidence;
    npk_u64 object_identity;
    npk_u64 object_scratch;
    npk_u64 object_name[4];
    npk_u64 object_handle;
    npk_u64 object_namespace;
    /* Milestone 6 syscall arguments and committed-evidence helpers. */
    npk_u64 syscall_number;
    npk_u64 syscall_error;
    npk_u64 syscall_arg[6];
    npk_u64 syscall_child_receipt;
};

/* Milestone 6 syscall integration points used by the processing core. */
npk_u64 npk_syscall_validate(struct NpkOperation *op);
npk_u64 npk_syscall_execute(struct NpkOperation *op);
npk_u64 npk_syscall_commit(struct NpkOperation *op);

/* Milestone 5 object payload services over Milestone 4 memory state. */
int npk_memory_region_owned_by(npk_u64 region_id, npk_u64 owner);
int npk_memory_address_space_owned_by(npk_u64 index, npk_u64 owner);
int npk_memory_release_for_object(npk_u64 region_id, npk_u64 owner);

/* Milestone 5 object manager integration points used by the processing core. */
void npk_core_set_object_name(npk_u64 slot, npk_u64 word0, npk_u64 word1,
                              npk_u64 word2, npk_u64 word3);
void npk_core_set_syscall_arguments(npk_u64 slot, npk_u64 number,
                                    npk_u64 error, npk_u64 *args);
npk_u64 npk_core_operation_syscall_error(npk_u64 slot);
npk_u64 npk_core_operation_syscall_child_receipt(npk_u64 slot);
void npk_object_clear_receipt_ext(npk_u64 index);
void npk_object_sync_receipt_ext(struct NpkOperation *op, npk_u64 index);
int npk_object_selftest(void);
npk_u64 npk_object_handle_for_id(npk_u64 object_id, npk_u64 owner);
npk_u64 npk_object_id_for_handle(npk_u64 handle, npk_u64 owner);
npk_u64 npk_object_payload_for_handle(npk_u64 handle, npk_u64 owner);

/* Milestone 6 typed bridge to the Milestone 5 object request path. */
struct NpkObjectSyscallRequest {
    npk_u64 operation;
    npk_u64 id;
    npk_u64 owner;
    npk_u64 type;
    npk_u64 handle;
    npk_u64 permissions;
    npk_u64 payload;
    npk_u64 parent;
    npk_u64 aux;
    npk_u64 name[4];
};

int npk_object_syscall_request(struct NpkObjectSyscallRequest *request,
                               npk_u64 *out_result, npk_u64 *out_reason,
                               npk_u64 *out_receipt);

/* Milestone 6 user-pointer helpers over Milestone 4 page tables. */
npk_u64 npk_memory_user_physical(npk_u64 address, npk_u64 access,
                                 npk_u64 *out_physical);

#endif
