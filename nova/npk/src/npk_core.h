#ifndef NPK_CORE_H
#define NPK_CORE_H

typedef unsigned long npk_u64;

#define NPK_NO_INDEX 0xFFFFFFFFFFFFFFFF

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
};

/* Milestone 5 object payload services over Milestone 4 memory state. */
int npk_memory_region_owned_by(npk_u64 region_id, npk_u64 owner);
int npk_memory_address_space_owned_by(npk_u64 index, npk_u64 owner);
int npk_memory_release_for_object(npk_u64 region_id, npk_u64 owner);

/* Milestone 5 object manager integration points used by the processing core. */
void npk_core_set_object_name(npk_u64 slot, npk_u64 word0, npk_u64 word1,
                              npk_u64 word2, npk_u64 word3);
void npk_object_clear_receipt_ext(npk_u64 index);
void npk_object_sync_receipt_ext(struct NpkOperation *op, npk_u64 index);
int npk_object_selftest(void);

#endif
