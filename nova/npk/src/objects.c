/*
 * NPK Milestone 5 object manager and universal object model.
 *
 * Kernel-managed resources are represented through one contract: a typed
 * header with stable identity, ownership, explicit references and handles,
 * access policy, optional namespace membership, and type-specific destruction.
 * This is a kernel policy layer over the shared processing core and the
 * Milestone 4 memory manager. It adds no syscall, ISA, ABI, or object-file
 * format change. Every supported operation is admitted as a typed NPK
 * operation and traverses the full five-stage lifecycle with its own receipts.
 */

#include "npk_core.h"

#define NPK_OBJECT_STATE_ADDRESS 0x96060
#define NPK_OBJECT_TABLE_ADDRESS 0x960E0
#define NPK_HANDLE_TABLE_ADDRESS 0x980E0
#define NPK_OBJECT_REQUEST_ADDRESS 0x9A0E0
#define NPK_OBJECT_RESPONSE_ADDRESS 0x9A150
#define NPK_TYPE_REGISTRY_ADDRESS 0x9E190
#define NPK_NAMESPACE_ENTRY_TABLE_ADDRESS 0x9E390
#define NPK_NAMESPACE_STATE_ADDRESS 0x9E990
#define NPK_OBJECT_RECEIPT_EXT_ADDRESS 0x9EA10

#define NPK_OBJECT_STATE_MAGIC 0x4E504B4F
#define NPK_OBJECT_STATE_VERSION 5
#define NPK_TYPE_MAGIC 0x4E504B54
#define NPK_NAMESPACE_MAGIC 0x4E504B4E
#define NPK_OBJECT_REQUEST_MAGIC 0x4E504B51
#define NPK_OBJECT_RESPONSE_MAGIC 0x4E504B5A
#define NPK_OBJECT_ID_BASE 0x4E504B4F00000000

#define NPK_OP_OBJECT_CREATE 9
#define NPK_OP_OBJECT_DESTROY 10
#define NPK_OP_OBJECT_OPEN 11
#define NPK_OP_OBJECT_CLOSE 12
#define NPK_OP_OBJECT_QUERY 13
#define NPK_OP_OBJECT_RETAIN 14
#define NPK_OP_OBJECT_RELEASE 15
#define NPK_OP_OBJECT_DIRECTORY_INSERT 16
#define NPK_OP_OBJECT_DIRECTORY_REMOVE 17
#define NPK_OP_OBJECT_DIRECTORY_LOOKUP 18
#define NPK_OP_OBJECT_TYPE_REGISTER 19
#define NPK_OP_OBJECT_TYPE_QUERY 20

#define NPK_REASON_NONE 0
#define NPK_REASON_MEMORY_STATE 25
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

#define NPK_OUTCOME_PENDING 1
#define NPK_OUTCOME_COMPLETE 2
#define NPK_OUTCOME_REJECTED 3
#define NPK_OUTCOME_FAILED 4

#define NPK_REQUEST_OK 0
#define NPK_REQUEST_BAD_REQUEST 1
#define NPK_REQUEST_BAD_OP 2
#define NPK_REQUEST_ADMIT_FAIL 3
#define NPK_REQUEST_ADVANCE_FAIL 4
#define NPK_REQUEST_BAD_OUTCOME 5
#define NPK_REQUEST_REJECTED 6
#define NPK_REQUEST_FAILED 7

#define NPK_OBJECT_SLOTS 64
#define NPK_OBJECT_HEADER_SIZE 128
#define NPK_HANDLE_OWNERS 2
#define NPK_HANDLE_SLOTS 64
#define NPK_HANDLE_SLOT_SIZE 64
#define NPK_TYPE_SLOTS 8
#define NPK_TYPE_DESCRIPTOR_SIZE 64
#define NPK_NAMESPACE_ENTRY_SLOTS 16
#define NPK_NAMESPACE_ENTRY_SIZE 96
#define NPK_OBJECT_RECEIPT_SLOTS 128
#define NPK_OBJECT_RECEIPT_EXT_SIZE 32
#define NPK_NAME_WORDS 4
#define NPK_MAX_PATH_BYTES 32
#define NPK_MAX_COMPONENT_BYTES 16

#define NPK_OBJECT_STATE_FREE 0
#define NPK_OBJECT_STATE_ACTIVE 1
#define NPK_HANDLE_STATE_FREE 0
#define NPK_HANDLE_STATE_ACTIVE 1
#define NPK_TYPE_STATE_FREE 0
#define NPK_TYPE_STATE_ACTIVE 1
#define NPK_NAMESPACE_ENTRY_FREE 0
#define NPK_NAMESPACE_ENTRY_ACTIVE 1

#define NPK_OBJECT_TYPE_GENERIC 1
#define NPK_OBJECT_TYPE_MEMORY_REGION 2
#define NPK_OBJECT_TYPE_ADDRESS_SPACE 3
#define NPK_OBJECT_TYPE_DIRECTORY 4

#define NPK_OBJ_PERM_READ 0x01
#define NPK_OBJ_PERM_WRITE 0x02
#define NPK_OBJ_PERM_EXEC 0x04
#define NPK_OBJ_PERM_SHARE 0x08
#define NPK_OBJ_PERM_DESTROY 0x10
#define NPK_OBJ_PERM_ALL 0x1F

#define NPK_HANDLE_OWNER_SHIFT 56
#define NPK_HANDLE_GENERATION_SHIFT 16
#define NPK_HANDLE_INDEX_MASK 0xFFFF
#define NPK_HANDLE_GENERATION_MASK 0xFFFF

#define NPK_DESTROY_NONE 0
#define NPK_DESTROY_MEMORY_REGION 1
#define NPK_DESTROY_DIRECTORY 2

struct NpkObjectState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 next_id;
    npk_u64 next_epoch;
    npk_u64 object_count;
    npk_u64 handle_count;
    npk_u64 last_status;
    npk_u64 last_reason;
    npk_u64 last_object;
    npk_u64 last_handle;
    npk_u64 last_epoch;
    npk_u64 object_slots;
    npk_u64 handle_owners;
    npk_u64 handle_slots;
    npk_u64 selftest_status;
};

struct NpkObjectHeader {
    npk_u64 magic;
    npk_u64 id;
    npk_u64 type;
    npk_u64 owner;
    npk_u64 parent;
    npk_u64 permissions;
    npk_u64 refcount;
    npk_u64 handle_count;
    npk_u64 retain_count;
    npk_u64 payload;
    npk_u64 created_epoch;
    npk_u64 state;
    npk_u64 namespace_count;
    npk_u64 body[3];
};

struct NpkHandleSlot {
    npk_u64 state;
    npk_u64 generation;
    npk_u64 object_id;
    npk_u64 permissions;
    npk_u64 reserved[4];
};

struct NpkObjectRequest {
    npk_u64 magic;
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

struct NpkObjectResponse {
    npk_u64 magic;
    npk_u64 status;
    npk_u64 operation;
    npk_u64 reason;
    npk_u64 result;
    npk_u64 aux;
    npk_u64 receipt_index;
    npk_u64 slot;
};

struct NpkTypeDescriptor {
    npk_u64 state;
    npk_u64 id;
    npk_u64 name[2];
    npk_u64 body_size;
    npk_u64 valid_ops;
    npk_u64 allowed_access;
    npk_u64 destroy_action;
    npk_u64 reserved;
};

struct NpkNamespaceEntry {
    npk_u64 state;
    npk_u64 id;
    npk_u64 parent_id;
    npk_u64 object_id;
    npk_u64 name[4];
    npk_u64 owner;
    npk_u64 created_epoch;
    npk_u64 generation;
    npk_u64 reserved;
};

struct NpkNamespaceState {
    npk_u64 magic;
    npk_u64 version;
    npk_u64 init_status;
    npk_u64 root_id;
    npk_u64 entry_count;
    npk_u64 next_id;
    npk_u64 next_epoch;
    npk_u64 last_status;
    npk_u64 last_reason;
    npk_u64 last_parent;
    npk_u64 last_object;
    npk_u64 last_entry;
    npk_u64 reserved[4];
};

struct NpkReceiptExt {
    npk_u64 object_id;
    npk_u64 handle;
    npk_u64 counts;
    npk_u64 namespace_id;
};

extern int npk_core_admit(npk_u64 type, npk_u64 id, npk_u64 input,
                          npk_u64 *out_slot);
extern int npk_core_advance(npk_u64 slot);
extern int npk_core_release(npk_u64 slot);
extern int npk_core_operation_outcome(npk_u64 slot);
extern npk_u64 npk_core_operation_result(npk_u64 slot);
extern npk_u64 npk_core_operation_reason(npk_u64 slot);
extern npk_u64 npk_core_operation_receipt_index(npk_u64 slot);
extern void npk_core_set_object_arguments(npk_u64 slot, npk_u64 type,
                                          npk_u64 flags, npk_u64 parent,
                                          npk_u64 payload);

npk_u64 npk_object_validate(struct NpkOperation *op);
npk_u64 npk_object_execute(struct NpkOperation *op);
npk_u64 npk_object_commit(struct NpkOperation *op);
npk_u64 npk_find_object(npk_u64 id);

static struct NpkObjectState *npk_state(void)
{
    return (struct NpkObjectState *)NPK_OBJECT_STATE_ADDRESS;
}

static struct NpkObjectHeader *npk_object(npk_u64 index)
{
    return (struct NpkObjectHeader *)(NPK_OBJECT_TABLE_ADDRESS +
        index * NPK_OBJECT_HEADER_SIZE);
}

static struct NpkHandleSlot *npk_handle_slot(npk_u64 owner, npk_u64 index)
{
    npk_u64 linear = (owner - 1) * NPK_HANDLE_SLOTS + index;
    return (struct NpkHandleSlot *)(NPK_HANDLE_TABLE_ADDRESS +
        linear * NPK_HANDLE_SLOT_SIZE);
}

static struct NpkObjectRequest *npk_request(void)
{
    return (struct NpkObjectRequest *)NPK_OBJECT_REQUEST_ADDRESS;
}

static struct NpkObjectResponse *npk_response(void)
{
    return (struct NpkObjectResponse *)NPK_OBJECT_RESPONSE_ADDRESS;
}

static struct NpkTypeDescriptor *npk_type(npk_u64 id)
{
    if (id == 0 || id > NPK_TYPE_SLOTS) {
        return 0;
    }
    return (struct NpkTypeDescriptor *)(NPK_TYPE_REGISTRY_ADDRESS +
        (id - 1) * NPK_TYPE_DESCRIPTOR_SIZE);
}

static struct NpkNamespaceEntry *npk_namespace_entry(npk_u64 index)
{
    return (struct NpkNamespaceEntry *)(NPK_NAMESPACE_ENTRY_TABLE_ADDRESS +
        index * NPK_NAMESPACE_ENTRY_SIZE);
}

static struct NpkNamespaceState *npk_namespace_state(void)
{
    return (struct NpkNamespaceState *)NPK_NAMESPACE_STATE_ADDRESS;
}

static struct NpkReceiptExt *npk_receipt_ext(npk_u64 index)
{
    return (struct NpkReceiptExt *)(NPK_OBJECT_RECEIPT_EXT_ADDRESS +
        index * NPK_OBJECT_RECEIPT_EXT_SIZE);
}

void npk_object_clear_receipt_ext(npk_u64 index)
{
    struct NpkReceiptExt *ext;

    if (index >= NPK_OBJECT_RECEIPT_SLOTS) {
        return;
    }
    ext = npk_receipt_ext(index);
    ext->object_id = 0;
    ext->handle = 0;
    ext->counts = 0;
    ext->namespace_id = 0;
}

void npk_object_sync_receipt_ext(struct NpkOperation *op, npk_u64 index)
{
    struct NpkReceiptExt *ext;
    struct NpkObjectHeader *header;
    npk_u64 object_index;

    if (index >= NPK_OBJECT_RECEIPT_SLOTS) {
        return;
    }
    ext = npk_receipt_ext(index);
    object_index = npk_find_object(op->object_identity);
    ext->object_id = op->object_identity;
    ext->handle = op->object_handle;
    ext->namespace_id = op->object_namespace;
    if (object_index == NPK_NO_INDEX) {
        ext->counts = 0;
        return;
    }
    header = npk_object(object_index);
    ext->object_id = header->id;
    ext->counts = (header->refcount << 32) | header->handle_count;
}

static npk_u64 npk_encode_handle(npk_u64 owner, npk_u64 generation,
                                 npk_u64 index)
{
    return (owner << NPK_HANDLE_OWNER_SHIFT) |
           (generation << NPK_HANDLE_GENERATION_SHIFT) | index;
}

static int npk_decode_handle(npk_u64 handle, npk_u64 *owner,
                             npk_u64 *generation, npk_u64 *index)
{
    *owner = handle >> NPK_HANDLE_OWNER_SHIFT;
    *generation = (handle >> NPK_HANDLE_GENERATION_SHIFT) &
                  NPK_HANDLE_GENERATION_MASK;
    *index = handle & NPK_HANDLE_INDEX_MASK;
    if (*owner < 1 || *owner > NPK_HANDLE_OWNERS) {
        return 0;
    }
    if (*index >= NPK_HANDLE_SLOTS || *generation == 0) {
        return 0;
    }
    return 1;
}

npk_u64 npk_find_object(npk_u64 id)
{
    struct NpkObjectState *state = npk_state();
    npk_u64 i;

    if (id == 0) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < state->object_slots; i = i + 1) {
        if (npk_object(i)->state == NPK_OBJECT_STATE_ACTIVE &&
                npk_object(i)->id == id) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_free_object(void)
{
    struct NpkObjectState *state = npk_state();
    npk_u64 i;

    for (i = 0; i < state->object_slots; i = i + 1) {
        if (npk_object(i)->state == NPK_OBJECT_STATE_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_free_handle(npk_u64 owner)
{
    npk_u64 i;

    if (owner < 1 || owner > NPK_HANDLE_OWNERS) {
        return NPK_NO_INDEX;
    }
    for (i = 0; i < NPK_HANDLE_SLOTS; i = i + 1) {
        if (npk_handle_slot(owner, i)->state == NPK_HANDLE_STATE_FREE) {
            return i;
        }
    }
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_namespace_entry(npk_u64 parent_id, npk_u64 *name,
                                        npk_u64 *index_out)
{
    struct NpkNamespaceState *ns = npk_namespace_state();
    npk_u64 i;
    npk_u64 word;
    struct NpkNamespaceEntry *entry;

    for (i = 0; i < ns->next_id; i = i + 1) {
        if (i >= NPK_NAMESPACE_ENTRY_SLOTS) {
            break;
        }
        entry = npk_namespace_entry(i);
        if (entry->state != NPK_NAMESPACE_ENTRY_ACTIVE ||
                entry->parent_id != parent_id) {
            continue;
        }
        word = 0;
        while (word < NPK_NAME_WORDS) {
            if (entry->name[word] != name[word]) {
                break;
            }
            word = word + 1;
        }
        if (word == NPK_NAME_WORDS) {
            *index_out = i;
            return i;
        }
    }
    *index_out = NPK_NO_INDEX;
    return NPK_NO_INDEX;
}

static npk_u64 npk_find_namespace_entry_for_object(npk_u64 object_id,
                                                   npk_u64 *index_out)
{
    struct NpkNamespaceState *ns = npk_namespace_state();
    npk_u64 i;
    struct NpkNamespaceEntry *entry;

    for (i = 0; i < ns->next_id; i = i + 1) {
        if (i >= NPK_NAMESPACE_ENTRY_SLOTS) {
            break;
        }
        entry = npk_namespace_entry(i);
        if (entry->state == NPK_NAMESPACE_ENTRY_ACTIVE &&
                entry->object_id == object_id) {
            *index_out = i;
            return i;
        }
    }
    *index_out = NPK_NO_INDEX;
    return NPK_NO_INDEX;
}

static int npk_valid_permissions(npk_u64 permissions)
{
    if (permissions == 0 || (permissions & ~NPK_OBJ_PERM_ALL) != 0) {
        return 0;
    }
    return 1;
}

static int npk_name_char_valid(npk_u64 value)
{
    if (value >= 'a' && value <= 'z') {
        return 1;
    }
    if (value >= 'A' && value <= 'Z') {
        return 1;
    }
    if (value >= '0' && value <= '9') {
        return 1;
    }
    if (value == '_' || value == '-' || value == '.' || value == '/') {
        return 1;
    }
    return 0;
}

static npk_u64 npk_name_byte(npk_u64 *name, npk_u64 offset)
{
    npk_u64 word = offset >> 3;
    npk_u64 shift = (offset & 7) * 8;
    return (name[word] >> shift) & 0xFF;
}

static npk_u64 npk_name_length(npk_u64 *name)
{
    npk_u64 offset = 0;
    npk_u64 word = 0;

    while (word < NPK_NAME_WORDS) {
        if (name[word] == 0) {
            break;
        }
        word = word + 1;
    }
    if (word == NPK_NAME_WORDS) {
        return NPK_MAX_PATH_BYTES + 1;
    }
    if (word == 0) {
        return 0;
    }
    offset = (word - 1) * 8;
    while (offset < NPK_MAX_PATH_BYTES &&
            npk_name_byte(name, offset) != 0) {
        offset = offset + 1;
    }
    return offset;
}

static int npk_path_valid(npk_u64 *name, int single_component)
{
    npk_u64 length = npk_name_length(name);
    npk_u64 offset = 0;
    npk_u64 component = 0;
    npk_u64 component_length = 0;
    npk_u64 value;
    int saw_slash = 0;

    if (length == 0 || length > NPK_MAX_PATH_BYTES) {
        return 0;
    }
    if (single_component && npk_name_byte(name, 0) == '/') {
        return 0;
    }
    if (npk_name_byte(name, 0) == '/') {
        offset = 1;
        saw_slash = 1;
    }
    while (offset < length) {
        value = npk_name_byte(name, offset);
        if (value == '/') {
            if (single_component || component_length == 0 || saw_slash) {
                return 0;
            }
            component = component + 1;
            component_length = 0;
            saw_slash = 1;
        } else {
            if (npk_name_char_valid(value) == 0) {
                return 0;
            }
            component_length = component_length + 1;
            if (component_length > NPK_MAX_COMPONENT_BYTES) {
                return 0;
            }
            saw_slash = 0;
        }
        offset = offset + 1;
    }
    if (component_length == 0 || saw_slash) {
        return 0;
    }
    if (single_component && component != 0) {
        return 0;
    }
    return 1;
}

static int npk_root_path(npk_u64 *name)
{
    if (npk_name_length(name) != 1) {
        return 0;
    }
    return npk_name_byte(name, 0) == '/';
}

static int npk_payload_valid(struct NpkTypeDescriptor *descriptor,
                             npk_u64 type, npk_u64 payload, npk_u64 owner)
{
    if (type == NPK_OBJECT_TYPE_MEMORY_REGION) {
        if (payload == 0) {
            return 0;
        }
        return npk_memory_region_owned_by(payload, owner);
    }
    if (type == NPK_OBJECT_TYPE_ADDRESS_SPACE) {
        return npk_memory_address_space_owned_by(payload, owner);
    }
    if (type == NPK_OBJECT_TYPE_DIRECTORY && descriptor->body_size != 8) {
        return 0;
    }
    return 1;
}

static int npk_parent_valid(npk_u64 parent, npk_u64 owner, npk_u64 type)
{
    npk_u64 index;
    struct NpkObjectHeader *header;

    if (parent == 0) {
        return type != NPK_OBJECT_TYPE_DIRECTORY;
    }
    index = npk_find_object(parent);
    if (index == NPK_NO_INDEX) {
        return 0;
    }
    header = npk_object(index);
    if (header->owner != owner) {
        return 0;
    }
    if (type == NPK_OBJECT_TYPE_DIRECTORY &&
            header->type != NPK_OBJECT_TYPE_DIRECTORY) {
        return 0;
    }
    return 1;
}

static int npk_handle_access(struct NpkHandleSlot *slot,
                             npk_u64 handle_owner, npk_u64 request_owner,
                             npk_u64 needed)
{
    if (needed == 0) {
        return 1;
    }
    if ((needed & ~slot->permissions) != 0) {
        return 0;
    }
    if (handle_owner != request_owner &&
            (slot->permissions & NPK_OBJ_PERM_SHARE) == 0) {
        return 0;
    }
    return 1;
}

static int npk_handle_object(npk_u64 handle, npk_u64 *owner,
                             npk_u64 *generation, npk_u64 *index,
                             npk_u64 *object_index)
{
    npk_u64 handle_index;
    struct NpkHandleSlot *slot;

    if (npk_decode_handle(handle, owner, generation, &handle_index) == 0) {
        return 0;
    }
    slot = npk_handle_slot(*owner, handle_index);
    if (slot->state != NPK_HANDLE_STATE_ACTIVE ||
            slot->generation != *generation) {
        return 0;
    }
    *index = handle_index;
    *object_index = npk_find_object(slot->object_id);
    return *object_index != NPK_NO_INDEX;
}

static npk_u64 npk_install_handle(npk_u64 owner, npk_u64 index,
                                  npk_u64 object_index, npk_u64 permissions,
                                  npk_u64 *out_handle)
{
    struct NpkObjectState *state = npk_state();
    struct NpkHandleSlot *slot = npk_handle_slot(owner, index);
    struct NpkObjectHeader *header = npk_object(object_index);
    npk_u64 generation = slot->generation + 1;

    if (generation == 0 || generation > NPK_HANDLE_GENERATION_MASK) {
        generation = 1;
    }
    slot->state = NPK_HANDLE_STATE_ACTIVE;
    slot->generation = generation;
    slot->object_id = header->id;
    slot->permissions = permissions;
    *out_handle = npk_encode_handle(owner, generation, index);
    state->handle_count = state->handle_count + 1;
    header->handle_count = header->handle_count + 1;
    header->refcount = header->refcount + 1;
    return NPK_REASON_NONE;
}

static npk_u64 npk_clear_handle(npk_u64 handle, npk_u64 *object_index,
                                npk_u64 *owner)
{
    struct NpkObjectState *state = npk_state();
    npk_u64 generation;
    npk_u64 index;
    struct NpkHandleSlot *slot;
    struct NpkObjectHeader *header;

    if (npk_handle_object(handle, owner, &generation, &index,
                          object_index) == 0) {
        return NPK_REASON_OBJECT_BAD_HANDLE;
    }
    slot = npk_handle_slot(*owner, index);
    header = npk_object(*object_index);
    slot->state = NPK_HANDLE_STATE_FREE;
    slot->object_id = 0;
    slot->permissions = 0;
    state->handle_count = state->handle_count - 1;
    header->handle_count = header->handle_count - 1;
    header->refcount = header->refcount - 1;
    return NPK_REASON_NONE;
}

static npk_u64 npk_stage_status(int status)
{
    if (status == 0) {
        return NPK_REASON_NONE;
    }
    if (status == 3 || status == 4 || status == 6) {
        return NPK_REASON_OBJECT_BAD_REQUEST;
    }
    return NPK_REASON_OBJECT_STATE;
}

static int npk_type_valid_op(npk_u64 type, npk_u64 operation)
{
    struct NpkTypeDescriptor *descriptor = npk_type(type);

    if (descriptor == 0 || descriptor->state != NPK_TYPE_STATE_ACTIVE) {
        return 0;
    }
    if (operation == NPK_OP_OBJECT_DIRECTORY_INSERT ||
            operation == NPK_OP_OBJECT_DIRECTORY_REMOVE ||
            operation == NPK_OP_OBJECT_DIRECTORY_LOOKUP) {
        return type == NPK_OBJECT_TYPE_DIRECTORY;
    }
    if (operation == NPK_OP_OBJECT_CREATE &&
            descriptor->destroy_action == NPK_DESTROY_DIRECTORY) {
        return 1;
    }
    return 1;
}

static npk_u64 npk_type_valid_ops_all(void)
{
    return 0x3FFF;
}

static void npk_type_set(npk_u64 id, npk_u64 name0, npk_u64 name1,
                         npk_u64 body_size, npk_u64 allowed_access,
                         npk_u64 destroy_action)
{
    struct NpkTypeDescriptor *descriptor = npk_type(id);

    descriptor->state = NPK_TYPE_STATE_ACTIVE;
    descriptor->id = id;
    descriptor->name[0] = name0;
    descriptor->name[1] = name1;
    descriptor->body_size = body_size;
    descriptor->valid_ops = npk_type_valid_ops_all();
    descriptor->allowed_access = allowed_access;
    descriptor->destroy_action = destroy_action;
    descriptor->reserved = 0;
}

static int npk_type_init(void)
{
    npk_type_set(NPK_OBJECT_TYPE_GENERIC, 0x434952454e4547, 0, 0,
                 NPK_OBJ_PERM_ALL, NPK_DESTROY_NONE);
    npk_type_set(NPK_OBJECT_TYPE_MEMORY_REGION, 0x525f59524f4d454d,
                 0x4e4f494745, 8, NPK_OBJ_PERM_ALL,
                 NPK_DESTROY_MEMORY_REGION);
    npk_type_set(NPK_OBJECT_TYPE_ADDRESS_SPACE, 0x5f53534552444441,
                 0x4543415053, 8, NPK_OBJ_PERM_ALL, NPK_DESTROY_NONE);
    npk_type_set(NPK_OBJECT_TYPE_DIRECTORY, 0x524f544345524944, 0x59, 8,
                 NPK_OBJ_PERM_ALL, NPK_DESTROY_DIRECTORY);
    return 0;
}

static npk_u64 npk_namespace_clear_entry(npk_u64 index)
{
    struct NpkNamespaceEntry *entry = npk_namespace_entry(index);
    npk_u64 i;

    entry->state = NPK_NAMESPACE_ENTRY_FREE;
    entry->id = 0;
    entry->parent_id = 0;
    entry->object_id = 0;
    for (i = 0; i < NPK_NAME_WORDS; i = i + 1) {
        entry->name[i] = 0;
    }
    entry->owner = 0;
    entry->created_epoch = 0;
    return NPK_REASON_NONE;
}

static npk_u64 npk_namespace_install(npk_u64 parent_id, npk_u64 object_id,
                                     npk_u64 *name, npk_u64 owner,
                                     npk_u64 *out_index)
{
    struct NpkNamespaceState *ns = npk_namespace_state();
    struct NpkNamespaceEntry *entry;
    npk_u64 index = NPK_NO_INDEX;
    npk_u64 object_index = npk_find_object(object_id);
    struct NpkObjectHeader *header;
    npk_u64 i;

    if (ns->next_id >= NPK_NAMESPACE_ENTRY_SLOTS ||
            object_index == NPK_NO_INDEX) {
        return NPK_REASON_OBJECT_EXHAUSTED;
    }
    for (i = 0; i < NPK_NAMESPACE_ENTRY_SLOTS; i = i + 1) {
        if (npk_namespace_entry(i)->state == NPK_NAMESPACE_ENTRY_FREE) {
            index = i;
            break;
        }
    }
    if (index == NPK_NO_INDEX) {
        return NPK_REASON_OBJECT_EXHAUSTED;
    }
    entry = npk_namespace_entry(index);
    entry->state = NPK_NAMESPACE_ENTRY_ACTIVE;
    entry->id = ns->next_id;
    entry->parent_id = parent_id;
    entry->object_id = object_id;
    for (i = 0; i < NPK_NAME_WORDS; i = i + 1) {
        entry->name[i] = name[i];
    }
    entry->owner = owner;
    entry->created_epoch = ns->next_epoch;
    entry->generation = entry->generation + 1;
    if (entry->generation == 0) {
        entry->generation = 1;
    }
    header = npk_object(object_index);
    header->namespace_count = header->namespace_count + 1;
    header->refcount = header->refcount + 1;
    ns->entry_count = ns->entry_count + 1;
    ns->next_id = ns->next_id + 1;
    ns->next_epoch = ns->next_epoch + 1;
    ns->last_parent = parent_id;
    ns->last_object = object_id;
    ns->last_entry = entry->id;
    *out_index = index;
    return NPK_REASON_NONE;
}

static npk_u64 npk_namespace_remove(npk_u64 index)
{
    struct NpkNamespaceState *ns = npk_namespace_state();
    struct NpkNamespaceEntry *entry = npk_namespace_entry(index);
    npk_u64 object_index;
    struct NpkObjectHeader *header;

    if (entry->state != NPK_NAMESPACE_ENTRY_ACTIVE) {
        return NPK_REASON_OBJECT_NO_ENTRY;
    }
    object_index = npk_find_object(entry->object_id);
    if (object_index == NPK_NO_INDEX) {
        return NPK_REASON_OBJECT_BAD_OBJECT;
    }
    header = npk_object(object_index);
    if (header->namespace_count == 0 || header->refcount == 0) {
        return NPK_REASON_OBJECT_STATE;
    }
    header->namespace_count = header->namespace_count - 1;
    header->refcount = header->refcount - 1;
    ns->entry_count = ns->entry_count - 1;
    ns->last_parent = entry->parent_id;
    ns->last_object = entry->object_id;
    ns->last_entry = entry->id;
    ns->next_epoch = ns->next_epoch + 1;
    npk_namespace_clear_entry(index);
    return NPK_REASON_NONE;
}

static npk_u64 npk_resolve_path(npk_u64 start_id, npk_u64 *name,
                                npk_u64 owner, npk_u64 *out_object)
{
    npk_u64 current = start_id;
    npk_u64 length = npk_name_length(name);
    npk_u64 offset = 0;
    npk_u64 components[4][4];
    npk_u64 component_length = 0;
    npk_u64 component_count = 0;
    npk_u64 i;
    npk_u64 word;
    npk_u64 shift;
    npk_u64 value;
    npk_u64 entry_index;
    struct NpkNamespaceEntry *entry;
    struct NpkObjectHeader *header;

    if (npk_root_path(name)) {
        current = npk_namespace_state()->root_id;
        *out_object = current;
        return NPK_REASON_NONE;
    }
    if (length == 0 || length > NPK_MAX_PATH_BYTES) {
        return NPK_REASON_OBJECT_BAD_PATH;
    }
    if (npk_name_byte(name, 0) == '/') {
        current = npk_namespace_state()->root_id;
        offset = 1;
    }
    while (offset <= length) {
        if (offset == length) {
            value = '/';
        } else {
            value = npk_name_byte(name, offset);
        }
        if (value == '/') {
            if (component_length == 0 ||
                    component_length > NPK_MAX_COMPONENT_BYTES ||
                    component_count >= 4) {
                return NPK_REASON_OBJECT_BAD_PATH;
            }
            for (i = 0; i < 4; i = i + 1) {
                components[component_count][i] = 0;
            }
            for (i = 0; i < component_length; i = i + 1) {
                word = i >> 3;
                shift = (i & 7) * 8;
                components[component_count][word] =
                    components[component_count][word] |
                    (npk_name_byte(name, offset - component_length + i) << shift);
            }
            component_count = component_count + 1;
            component_length = 0;
        } else {
            if (npk_name_char_valid(value) == 0) {
                return NPK_REASON_OBJECT_BAD_PATH;
            }
            component_length = component_length + 1;
        }
        offset = offset + 1;
    }

    for (i = 0; i < component_count; i = i + 1) {
        word = npk_find_object(current);
        if (word == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        header = npk_object(word);
        if (i > 0 && header->type != NPK_OBJECT_TYPE_DIRECTORY) {
            return NPK_REASON_OBJECT_NOT_DIRECTORY;
        }
        if (header->owner != owner ||
                (header->permissions & NPK_OBJ_PERM_SHARE) == 0) {
            return NPK_REASON_OBJECT_BAD_OWNER;
        }
        if ((header->permissions & NPK_OBJ_PERM_READ) == 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (npk_find_namespace_entry(current, components[i], &entry_index) ==
                NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_NO_ENTRY;
        }
        entry = npk_namespace_entry(entry_index);
        current = entry->object_id;
    }
    *out_object = current;
    return NPK_REASON_NONE;
}

npk_u64 npk_object_validate(struct NpkOperation *op)
{
    struct NpkObjectRequest *request = npk_request();
    struct NpkTypeDescriptor *descriptor;
    npk_u64 owner;
    npk_u64 generation;
    npk_u64 index;
    npk_u64 object_index;
    npk_u64 entry_index;
    npk_u64 resolved;
    npk_u64 child_index;
    npk_u64 reason;
    struct NpkObjectHeader *header;
    struct NpkHandleSlot *slot;

    if (op->input != NPK_OBJECT_REQUEST_ADDRESS ||
            request->magic != NPK_OBJECT_REQUEST_MAGIC ||
            request->id != op->id || request->operation != op->type) {
        return NPK_REASON_OBJECT_BAD_REQUEST;
    }
    if (request->owner == 0 || request->owner > NPK_HANDLE_OWNERS) {
        return NPK_REASON_OBJECT_BAD_OWNER;
    }

    if (op->type == NPK_OP_OBJECT_TYPE_REGISTER) {
        if (request->handle != 0 || request->parent != 0 ||
                request->payload > 32 || request->aux != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        descriptor = npk_type(request->type);
        if (descriptor == 0) {
            return NPK_REASON_OBJECT_BAD_TYPE;
        }
        if (descriptor->state == NPK_TYPE_STATE_ACTIVE) {
            return NPK_REASON_OBJECT_DUPLICATE;
        }
        if (npk_valid_permissions(request->permissions) == 0 ||
                npk_path_valid(request->name, 1) == 0) {
            return NPK_REASON_OBJECT_BAD_NAME;
        }
        op->object_type = request->type;
        op->object_flags = request->permissions;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_TYPE_QUERY) {
        if (request->handle != 0 || request->permissions != 0 ||
                request->payload != 0 || request->parent != 0 ||
                request->aux != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        descriptor = npk_type(request->type);
        if (descriptor == 0 ||
                descriptor->state != NPK_TYPE_STATE_ACTIVE) {
            return NPK_REASON_OBJECT_BAD_TYPE;
        }
        op->object_type = request->type;
        op->object_evidence = descriptor->id;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_CREATE) {
        descriptor = npk_type(request->type);
        if (descriptor == 0 ||
                descriptor->state != NPK_TYPE_STATE_ACTIVE) {
            return NPK_REASON_OBJECT_BAD_TYPE;
        }
        if (npk_valid_permissions(request->permissions) == 0 ||
                (request->permissions & ~descriptor->allowed_access) != 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (npk_payload_valid(descriptor, request->type, request->payload,
                              request->owner) == 0) {
            return NPK_REASON_OBJECT_PAYLOAD;
        }
        if (npk_parent_valid(request->parent, request->owner,
                             request->type) == 0) {
            return NPK_REASON_OBJECT_BAD_PARENT;
        }
        if (request->handle != 0 || request->aux != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        if (npk_name_length(request->name) != 0) {
            if (request->parent == 0 ||
                    npk_path_valid(request->name, 1) == 0) {
                return NPK_REASON_OBJECT_BAD_NAME;
            }
            object_index = npk_find_object(request->parent);
            if (object_index == NPK_NO_INDEX ||
                    npk_object(object_index)->type !=
                    NPK_OBJECT_TYPE_DIRECTORY) {
                return NPK_REASON_OBJECT_NOT_DIRECTORY;
            }
            if (npk_find_namespace_entry(request->parent, request->name,
                                         &entry_index) != NPK_NO_INDEX) {
                return NPK_REASON_OBJECT_DUPLICATE;
            }
            op->object_namespace = request->parent;
            op->object_scratch = entry_index;
        }
        if (request->type == NPK_OBJECT_TYPE_DIRECTORY &&
                npk_name_length(request->name) == 0) {
            return NPK_REASON_OBJECT_BAD_NAME;
        }
        op->object_type = request->type;
        op->object_flags = request->permissions;
        op->object_parent = request->parent;
        op->object_payload = request->payload;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DESTROY) {
        if (request->handle != 0 || request->type != 0 ||
                request->permissions != 0 || request->aux != 0 ||
                npk_name_length(request->name) != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        object_index = npk_find_object(request->payload);
        if (object_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        header = npk_object(object_index);
        if (header->owner != request->owner) {
            return NPK_REASON_OBJECT_BAD_OWNER;
        }
        if ((header->permissions & NPK_OBJ_PERM_DESTROY) == 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (header->state != NPK_OBJECT_STATE_ACTIVE ||
                header->refcount != 0 || header->handle_count != 0 ||
                header->retain_count != 0 || header->namespace_count != 0 ||
                header->id == npk_namespace_state()->root_id) {
            return NPK_REASON_OBJECT_STATE;
        }
        if (npk_find_namespace_entry_for_object(header->id, &entry_index) !=
                NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_STATE;
        }
        op->object_type = header->type;
        op->object_evidence = request->payload;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT ||
            op->type == NPK_OP_OBJECT_DIRECTORY_REMOVE) {
        if (request->type != 0 || request->permissions != 0 ||
                request->parent != 0 || request->aux != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        if (npk_handle_object(request->handle, &owner, &generation, &index,
                              &object_index) == 0) {
            return NPK_REASON_OBJECT_BAD_HANDLE;
        }
        if (owner != request->owner) {
            return NPK_REASON_OBJECT_BAD_OWNER;
        }
        header = npk_object(object_index);
        slot = npk_handle_slot(owner, index);
        if (header->type != NPK_OBJECT_TYPE_DIRECTORY) {
            return NPK_REASON_OBJECT_NOT_DIRECTORY;
        }
        if (npk_handle_access(slot, owner, request->owner, NPK_OBJ_PERM_WRITE) == 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (npk_path_valid(request->name, 1) == 0) {
            return NPK_REASON_OBJECT_BAD_NAME;
        }
        if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT) {
            child_index = npk_find_object(request->payload);
            if (child_index == NPK_NO_INDEX) {
                return NPK_REASON_OBJECT_BAD_OBJECT;
            }
            if (npk_object(child_index)->owner != request->owner) {
                return NPK_REASON_OBJECT_BAD_OWNER;
            }
            if (npk_find_namespace_entry(header->id, request->name,
                                         &entry_index) != NPK_NO_INDEX) {
                return NPK_REASON_OBJECT_DUPLICATE;
            }
            op->object_evidence = request->payload;
        } else {
            if (request->payload != 0) {
                return NPK_REASON_OBJECT_BAD_REQUEST;
            }
            if (npk_find_namespace_entry(header->id, request->name,
                                         &entry_index) == NPK_NO_INDEX) {
                return NPK_REASON_OBJECT_NO_ENTRY;
            }
            op->object_evidence = npk_namespace_entry(entry_index)->object_id;
        }
        op->object_type = header->type;
        op->object_namespace = header->id;
        op->object_scratch = entry_index;
        op->object_handle = request->handle;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_LOOKUP) {
        if (request->handle != 0 || request->type != 0 ||
                request->permissions != 0 || request->parent != 0 ||
                request->aux != 0) {
            return NPK_REASON_OBJECT_BAD_REQUEST;
        }
        if (request->payload == 0) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        object_index = npk_find_object(request->payload);
        if (object_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        header = npk_object(object_index);
        if (header->type != NPK_OBJECT_TYPE_DIRECTORY) {
            return NPK_REASON_OBJECT_NOT_DIRECTORY;
        }
        if (header->owner != request->owner) {
            return NPK_REASON_OBJECT_BAD_OWNER;
        }
        if ((header->permissions & NPK_OBJ_PERM_READ) == 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (npk_root_path(request->name) == 0 &&
                npk_path_valid(request->name, 0) == 0) {
            return NPK_REASON_OBJECT_BAD_NAME;
        }
        reason = npk_resolve_path(request->payload, request->name,
                                  request->owner, &resolved);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        op->object_type = header->type;
        op->object_evidence = resolved;
        op->object_namespace = request->payload;
        return NPK_REASON_NONE;
    }

    if (request->type != 0 || request->payload != 0 ||
            request->parent != 0 || request->aux != 0 ||
            npk_name_length(request->name) != 0) {
        return NPK_REASON_OBJECT_BAD_REQUEST;
    }
    if (op->type != NPK_OP_OBJECT_OPEN && request->permissions != 0) {
        return NPK_REASON_OBJECT_BAD_REQUEST;
    }
    if (npk_handle_object(request->handle, &owner, &generation, &index,
                          &object_index) == 0) {
        return NPK_REASON_OBJECT_BAD_HANDLE;
    }
    header = npk_object(object_index);
    slot = npk_handle_slot(owner, index);
    if (owner != request->owner && op->type != NPK_OP_OBJECT_OPEN) {
        return NPK_REASON_OBJECT_BAD_OWNER;
    }
    if (header->state != NPK_OBJECT_STATE_ACTIVE) {
        return NPK_REASON_OBJECT_STATE;
    }
    op->object_type = header->type;
    op->object_evidence = header->id;
    op->object_payload = header->payload;
    op->object_handle = request->handle;

    if (op->type == NPK_OP_OBJECT_OPEN) {
        if (npk_valid_permissions(request->permissions) == 0 ||
                (request->permissions & ~slot->permissions) != 0 ||
                (request->permissions & ~header->permissions) != 0) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
        if (owner != request->owner &&
                ((slot->permissions & NPK_OBJ_PERM_SHARE) == 0 ||
                 (header->permissions & NPK_OBJ_PERM_SHARE) == 0)) {
            return NPK_REASON_OBJECT_BAD_PERMISSION;
        }
    }
    if (op->type == NPK_OP_OBJECT_QUERY &&
            npk_handle_access(slot, owner, request->owner, NPK_OBJ_PERM_READ) == 0) {
        return NPK_REASON_OBJECT_BAD_PERMISSION;
    }
    if (op->type == NPK_OP_OBJECT_RETAIN &&
            header->refcount >= 0xFFFFFFFF) {
        return NPK_REASON_OBJECT_EXHAUSTED;
    }
    if (op->type == NPK_OP_OBJECT_RELEASE && header->retain_count == 0) {
        return NPK_REASON_OBJECT_STATE;
    }
    return NPK_REASON_NONE;
}

npk_u64 npk_object_execute(struct NpkOperation *op)
{
    struct NpkObjectRequest *request = npk_request();
    struct NpkNamespaceState *ns = npk_namespace_state();
    npk_u64 owner;
    npk_u64 generation;
    npk_u64 index;
    npk_u64 object_index;
    npk_u64 handle_index;
    npk_u64 entry_index;
    npk_u64 resolved;
    struct NpkObjectState *state = npk_state();
    struct NpkObjectHeader *header;

    if (op->type == NPK_OP_OBJECT_TYPE_REGISTER ||
            op->type == NPK_OP_OBJECT_TYPE_QUERY) {
        op->provisional = op->object_type - 1;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_CREATE) {
        object_index = npk_find_free_object();
        handle_index = npk_find_free_handle(request->owner);
        if (object_index == NPK_NO_INDEX || handle_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_EXHAUSTED;
        }
        if (npk_name_length(request->name) != 0 &&
                ns->next_id >= NPK_NAMESPACE_ENTRY_SLOTS) {
            return NPK_REASON_OBJECT_EXHAUSTED;
        }
        op->provisional = object_index;
        op->object_scratch = handle_index;
        op->object_evidence = NPK_OBJECT_ID_BASE + state->next_id;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DESTROY) {
        object_index = npk_find_object(request->payload);
        if (object_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        header = npk_object(object_index);
        op->provisional = object_index;
        op->object_evidence = header->id;
        op->object_scratch = header->payload;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT ||
            op->type == NPK_OP_OBJECT_DIRECTORY_REMOVE) {
        if (npk_handle_object(request->handle, &owner, &generation, &index,
                              &object_index) == 0) {
            return NPK_REASON_OBJECT_BAD_HANDLE;
        }
        if (owner != request->owner) {
            return NPK_REASON_OBJECT_BAD_OWNER;
        }
        if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT &&
                ns->next_id >= NPK_NAMESPACE_ENTRY_SLOTS) {
            return NPK_REASON_OBJECT_EXHAUSTED;
        }
        op->provisional = object_index;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_LOOKUP) {
        object_index = npk_find_object(request->payload);
        if (object_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_BAD_OBJECT;
        }
        if (npk_resolve_path(request->payload, request->name,
                             request->owner, &resolved) != NPK_REASON_NONE) {
            return NPK_REASON_OBJECT_BAD_PATH;
        }
        op->provisional = object_index;
        op->object_evidence = resolved;
        return NPK_REASON_NONE;
    }

    if (npk_handle_object(request->handle, &owner, &generation, &index,
                          &object_index) == 0) {
        return NPK_REASON_OBJECT_BAD_HANDLE;
    }
    if (owner != request->owner && op->type != NPK_OP_OBJECT_OPEN) {
        return NPK_REASON_OBJECT_BAD_OWNER;
    }
    header = npk_object(object_index);
    op->provisional = object_index;
    op->object_evidence = header->id;
    op->object_scratch = index;
    op->object_handle = request->handle;

    if (op->type == NPK_OP_OBJECT_OPEN) {
        handle_index = npk_find_free_handle(request->owner);
        if (handle_index == NPK_NO_INDEX) {
            return NPK_REASON_OBJECT_EXHAUSTED;
        }
        op->object_scratch = handle_index;
    }
    return NPK_REASON_NONE;
}

static npk_u64 npk_type_destructor(struct NpkObjectHeader *header)
{
    struct NpkTypeDescriptor *descriptor = npk_type(header->type);
    npk_u64 i;
    struct NpkNamespaceEntry *entry;
    npk_u64 count = 0;

    if (descriptor == 0) {
        return NPK_REASON_OBJECT_BAD_TYPE;
    }
    if (descriptor->destroy_action == NPK_DESTROY_MEMORY_REGION) {
        return npk_memory_release_for_object(header->payload, header->owner);
    }
    if (descriptor->destroy_action == NPK_DESTROY_DIRECTORY) {
        for (i = 0; i < NPK_NAMESPACE_ENTRY_SLOTS; i = i + 1) {
            entry = npk_namespace_entry(i);
            if (entry->state == NPK_NAMESPACE_ENTRY_ACTIVE &&
                    entry->parent_id == header->id) {
                count = count + 1;
            }
        }
        if (count != 0) {
            return NPK_REASON_OBJECT_STATE;
        }
    }
    return NPK_REASON_NONE;
}

npk_u64 npk_object_commit(struct NpkOperation *op)
{
    struct NpkObjectRequest *request = npk_request();
    struct NpkObjectState *state = npk_state();
    struct NpkNamespaceState *ns = npk_namespace_state();
    struct NpkTypeDescriptor *descriptor;
    struct NpkObjectHeader *header;
    struct NpkHandleSlot *slot;
    npk_u64 object_index;
    npk_u64 handle_index;
    npk_u64 id;
    npk_u64 handle;
    npk_u64 reason;
    npk_u64 owner;
    npk_u64 generation;
    npk_u64 index;
    npk_u64 entry_index;

    if (op->type == NPK_OP_OBJECT_TYPE_REGISTER) {
        descriptor = npk_type(op->object_type);
        if (descriptor == 0 || op->provisional >= NPK_TYPE_SLOTS) {
            return NPK_REASON_OBJECT_BAD_TYPE;
        }
        if (descriptor->state == NPK_TYPE_STATE_ACTIVE) {
            return NPK_REASON_OBJECT_DUPLICATE;
        }
        descriptor->state = NPK_TYPE_STATE_ACTIVE;
        descriptor->id = op->object_type;
        descriptor->name[0] = request->name[0];
        descriptor->name[1] = request->name[1];
        descriptor->body_size = request->payload;
        descriptor->valid_ops = npk_type_valid_ops_all();
        descriptor->allowed_access = request->permissions;
        descriptor->destroy_action = NPK_DESTROY_NONE;
        op->result = op->object_type;
        op->object_evidence = op->object_type;
        op->object_identity = op->object_type;
        op->object_namespace = 0;
        state->last_epoch = state->next_epoch;
        state->next_epoch = state->next_epoch + 1;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_TYPE_QUERY) {
        descriptor = npk_type(op->object_type);
        if (descriptor == 0 ||
                descriptor->state != NPK_TYPE_STATE_ACTIVE) {
            return NPK_REASON_OBJECT_BAD_TYPE;
        }
        op->result = descriptor->id;
        op->object_evidence = descriptor->id;
        op->object_identity = descriptor->id;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_CREATE) {
        object_index = op->provisional;
        handle_index = op->object_scratch;
        id = NPK_OBJECT_ID_BASE + state->next_id;
        if (object_index >= state->object_slots ||
                handle_index >= NPK_HANDLE_SLOTS || id != op->object_evidence) {
            return NPK_REASON_OBJECT_STATE;
        }
        header = npk_object(object_index);
        header->magic = NPK_OBJECT_STATE_MAGIC;
        header->id = id;
        header->type = op->object_type;
        header->owner = request->owner;
        header->parent = op->object_parent;
        header->permissions = op->object_flags;
        header->refcount = 0;
        header->handle_count = 0;
        header->retain_count = 0;
        header->payload = op->object_payload;
        header->created_epoch = state->next_epoch;
        header->state = NPK_OBJECT_STATE_ACTIVE;
        header->namespace_count = 0;
        header->body[0] = 0;
        header->body[1] = 0;
        header->body[2] = 0;
        reason = npk_install_handle(request->owner, handle_index, object_index,
                                    op->object_flags, &handle);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        if (npk_name_length(request->name) != 0) {
            reason = npk_namespace_install(op->object_namespace, id,
                                           request->name, request->owner,
                                           &entry_index);
            if (reason != NPK_REASON_NONE) {
                npk_clear_handle(handle, &object_index, &owner);
                header->magic = 0;
                header->id = 0;
                header->state = NPK_OBJECT_STATE_FREE;
                return reason;
            }
            op->object_namespace = ns->last_entry;
        }
        state->next_id = state->next_id + 1;
        state->object_count = state->object_count + 1;
        state->last_object = id;
        state->last_handle = handle;
        state->last_epoch = state->next_epoch;
        state->next_epoch = state->next_epoch + 1;
        op->result = id;
        op->object_evidence = id;
        op->object_identity = id;
        op->object_handle = handle;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DESTROY) {
        object_index = op->provisional;
        if (object_index >= state->object_slots) {
            return NPK_REASON_OBJECT_STATE;
        }
        header = npk_object(object_index);
        id = header->id;
        if (header->state != NPK_OBJECT_STATE_ACTIVE ||
                header->owner != request->owner || id != request->payload ||
                header->refcount != 0) {
            return NPK_REASON_OBJECT_STATE;
        }
        reason = npk_type_destructor(header);
        if (reason != 0) {
            if (reason == NPK_REASON_MEMORY_STATE) {
                return NPK_REASON_OBJECT_STATE;
            }
            return NPK_REASON_OBJECT_PAYLOAD;
        }
        header->magic = 0;
        header->id = 0;
        header->type = 0;
        header->owner = 0;
        header->parent = 0;
        header->permissions = 0;
        header->refcount = 0;
        header->handle_count = 0;
        header->retain_count = 0;
        header->payload = 0;
        header->created_epoch = 0;
        header->state = NPK_OBJECT_STATE_FREE;
        header->namespace_count = 0;
        header->body[0] = 0;
        header->body[1] = 0;
        header->body[2] = 0;
        state->object_count = state->object_count - 1;
        state->last_object = id;
        state->last_handle = 0;
        state->last_epoch = state->next_epoch;
        state->next_epoch = state->next_epoch + 1;
        op->result = id;
        op->object_evidence = id;
        op->object_identity = id;
        op->object_handle = 0;
        op->object_namespace = 0;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT ||
            op->type == NPK_OP_OBJECT_DIRECTORY_REMOVE) {
        header = npk_object(op->provisional);
        op->object_type = header->type;
        op->object_namespace = header->id;
        op->object_identity = header->id;
        op->object_handle = request->handle;
        if (op->type == NPK_OP_OBJECT_DIRECTORY_INSERT) {
            reason = npk_namespace_install(header->id, request->payload,
                                           request->name, request->owner,
                                           &entry_index);
            if (reason != NPK_REASON_NONE) {
                return reason;
            }
            op->object_evidence = request->payload;
            op->object_namespace = ns->last_entry;
            op->result = ns->last_entry;
        } else {
            object_index = NPK_NO_INDEX;
            if (npk_find_namespace_entry(header->id, request->name,
                                         &object_index) == NPK_NO_INDEX) {
                return NPK_REASON_OBJECT_NO_ENTRY;
            }
            op->object_evidence =
                npk_namespace_entry(object_index)->object_id;
            reason = npk_namespace_remove(object_index);
            if (reason != NPK_REASON_NONE) {
                return reason;
            }
            op->object_namespace = ns->last_entry;
            op->result = ns->last_entry;
        }
        state->last_object = op->object_evidence;
        state->last_epoch = state->next_epoch;
        state->next_epoch = state->next_epoch + 1;
        return NPK_REASON_NONE;
    }

    if (op->type == NPK_OP_OBJECT_DIRECTORY_LOOKUP) {
        op->object_identity = op->object_evidence;
        op->result = op->object_evidence;
        state->last_object = op->object_evidence;
        state->last_epoch = state->next_epoch;
        state->next_epoch = state->next_epoch + 1;
        return NPK_REASON_NONE;
    }

    if (npk_handle_object(request->handle, &owner, &generation, &index,
                          &object_index) == 0) {
        return NPK_REASON_OBJECT_BAD_HANDLE;
    }
    if (owner != request->owner && op->type != NPK_OP_OBJECT_OPEN) {
        return NPK_REASON_OBJECT_BAD_OWNER;
    }
    header = npk_object(object_index);
    op->object_identity = header->id;
    op->object_handle = request->handle;

    if (op->type == NPK_OP_OBJECT_OPEN) {
        handle_index = op->object_scratch;
        if (handle_index >= NPK_HANDLE_SLOTS) {
            return NPK_REASON_OBJECT_STATE;
        }
        reason = npk_install_handle(request->owner, handle_index, object_index,
                                    request->permissions, &handle);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        state->last_handle = handle;
        op->result = handle;
        op->object_handle = handle;
    } else if (op->type == NPK_OP_OBJECT_CLOSE) {
        reason = npk_clear_handle(request->handle, &object_index, &owner);
        if (reason != NPK_REASON_NONE) {
            return reason;
        }
        state->last_handle = request->handle;
        op->result = header->id;
        op->object_handle = 0;
    } else if (op->type == NPK_OP_OBJECT_RETAIN) {
        header->retain_count = header->retain_count + 1;
        header->refcount = header->refcount + 1;
        op->result = header->id;
    } else if (op->type == NPK_OP_OBJECT_RELEASE) {
        if (header->retain_count == 0) {
            return NPK_REASON_OBJECT_STATE;
        }
        header->retain_count = header->retain_count - 1;
        header->refcount = header->refcount - 1;
        op->result = header->id;
    } else if (op->type == NPK_OP_OBJECT_QUERY) {
        op->result = header->id;
        op->object_evidence = header->payload;
    } else {
        return NPK_REASON_OBJECT_UNSUPPORTED;
    }

    state->last_object = header->id;
    state->last_epoch = state->next_epoch;
    state->next_epoch = state->next_epoch + 1;
    return NPK_REASON_NONE;
}

int npk_object_selftest(void)
{
    struct NpkObjectState *state = npk_state();
    struct NpkNamespaceState *ns = npk_namespace_state();
    struct NpkObjectHeader *root;
    struct NpkTypeDescriptor *descriptor;
    npk_u64 root_index;
    npk_u64 i;

    if (state->magic != NPK_OBJECT_STATE_MAGIC ||
            state->version != NPK_OBJECT_STATE_VERSION ||
            state->init_status != 0) {
        state->selftest_status = 1;
        return 1;
    }
    root_index = npk_find_object(ns->root_id);
    if (root_index == NPK_NO_INDEX) {
        state->selftest_status = 2;
        return 2;
    }
    root = npk_object(root_index);
    if (root->type != NPK_OBJECT_TYPE_DIRECTORY ||
            root->state != NPK_OBJECT_STATE_ACTIVE ||
            root->namespace_count != 1) {
        state->selftest_status = 3;
        return 3;
    }
    for (i = 1; i <= NPK_OBJECT_TYPE_DIRECTORY; i = i + 1) {
        descriptor = npk_type(i);
        if (descriptor == 0 ||
                descriptor->state != NPK_TYPE_STATE_ACTIVE ||
                descriptor->id != i) {
            state->selftest_status = 4;
            return 4;
        }
    }
    state->selftest_status = 0;
    return 0;
}

int npk_object_init(void)
{
    struct NpkObjectState *state = npk_state();
    struct NpkNamespaceState *ns = npk_namespace_state();
    struct NpkObjectHeader *root;
    struct NpkNamespaceEntry *entry;
    struct NpkTypeDescriptor *descriptor;
    npk_u64 i;

    for (i = 0; i < 16; i = i + 1) {
        *((npk_u64 *)state + i) = 0;
    }
    /* Fresh kernel memory is zero. Only the type registry needs explicit
     * clearing because object types are registered immediately below. */
    for (i = 0; i < NPK_TYPE_SLOTS; i = i + 1) {
        descriptor = (struct NpkTypeDescriptor *)(NPK_TYPE_REGISTRY_ADDRESS +
            i * NPK_TYPE_DESCRIPTOR_SIZE);
        descriptor->state = NPK_TYPE_STATE_FREE;
        descriptor->id = 0;
        descriptor->name[0] = 0;
        descriptor->name[1] = 0;
        descriptor->body_size = 0;
        descriptor->valid_ops = 0;
        descriptor->allowed_access = 0;
        descriptor->destroy_action = 0;
        descriptor->reserved = 0;
    }
    npk_type_init();

    state->magic = NPK_OBJECT_STATE_MAGIC;
    state->version = NPK_OBJECT_STATE_VERSION;
    state->init_status = 0;
    state->next_id = 1;
    state->next_epoch = 1;
    state->object_count = 1;
    state->handle_count = 0;
    state->object_slots = NPK_OBJECT_SLOTS;
    state->handle_owners = NPK_HANDLE_OWNERS;
    state->handle_slots = NPK_HANDLE_SLOTS;

    ns->magic = NPK_NAMESPACE_MAGIC;
    ns->version = NPK_OBJECT_STATE_VERSION;
    ns->init_status = 0;
    ns->root_id = NPK_OBJECT_ID_BASE;
    ns->entry_count = 1;
    ns->next_id = 2;
    ns->next_epoch = 2;

    root = npk_object(0);
    root->magic = NPK_OBJECT_STATE_MAGIC;
    root->id = ns->root_id;
    root->type = NPK_OBJECT_TYPE_DIRECTORY;
    root->owner = 1;
    root->parent = 0;
    root->permissions = NPK_OBJ_PERM_ALL;
    root->refcount = 1;
    root->handle_count = 0;
    root->retain_count = 0;
    root->payload = 0;
    root->created_epoch = 1;
    root->state = NPK_OBJECT_STATE_ACTIVE;
    root->namespace_count = 1;

    entry = npk_namespace_entry(0);
    entry->state = NPK_NAMESPACE_ENTRY_ACTIVE;
    entry->id = 1;
    entry->parent_id = ns->root_id;
    entry->object_id = ns->root_id;
    entry->name[0] = '/';
    entry->name[1] = 0;
    entry->name[2] = 0;
    entry->name[3] = 0;
    entry->owner = 1;
    entry->created_epoch = 1;
    entry->generation = 1;

    return npk_object_selftest();
}

int npk_object_request(void)
{
    struct NpkObjectState *state = npk_state();
    struct NpkObjectRequest *request = npk_request();
    struct NpkObjectResponse *response = npk_response();
    npk_u64 slot = NPK_NO_INDEX;
    npk_u64 type;
    npk_u64 reason = NPK_REASON_NONE;
    int status = 0;
    int outcome = NPK_OUTCOME_PENDING;
    int i;

    for (i = 0; i < 8; i = i + 1) {
        *((npk_u64 *)response + i) = 0;
    }
    response->magic = NPK_OBJECT_RESPONSE_MAGIC;
    response->operation = request->operation;
    state->last_status = 0;
    state->last_reason = NPK_REASON_NONE;

    if (request->magic != NPK_OBJECT_REQUEST_MAGIC || request->id == 0 ||
            request->owner == 0 || request->owner > NPK_HANDLE_OWNERS) {
        state->last_status = NPK_REQUEST_BAD_REQUEST;
        response->status = NPK_REQUEST_BAD_REQUEST;
        return NPK_REQUEST_BAD_REQUEST;
    }
    type = request->operation;
    if (type < NPK_OP_OBJECT_CREATE || type > NPK_OP_OBJECT_TYPE_QUERY) {
        state->last_status = NPK_REQUEST_BAD_OP;
        response->status = NPK_REQUEST_BAD_OP;
        return NPK_REQUEST_BAD_OP;
    }

    status = npk_core_admit(type, request->id, NPK_OBJECT_REQUEST_ADDRESS,
                            &slot);
    if (status != 0) {
        state->last_status = NPK_REQUEST_ADMIT_FAIL;
        state->last_reason = npk_stage_status(status);
        response->status = NPK_REQUEST_ADMIT_FAIL;
        response->reason = state->last_reason;
        return NPK_REQUEST_ADMIT_FAIL;
    }
    npk_core_set_object_arguments(slot, request->type, request->permissions,
                                  request->parent, request->payload);
    npk_core_set_object_name(slot, request->name[0], request->name[1],
                             request->name[2], request->name[3]);
    for (i = 0; i < 5; i = i + 1) {
        outcome = npk_core_operation_outcome(slot);
        if (outcome != NPK_OUTCOME_PENDING) {
            break;
        }
        status = npk_core_advance(slot);
        if (status != 0) {
            state->last_status = NPK_REQUEST_ADVANCE_FAIL;
            break;
        }
    }

    reason = npk_core_operation_reason(slot);
    response->result = npk_core_operation_result(slot);
    response->aux = npk_core_operation_receipt_index(slot);
    response->receipt_index = npk_core_operation_receipt_index(slot);
    response->slot = slot;
    response->reason = reason;
    state->last_reason = reason;

    if (state->last_status == 0 && status != 0) {
        state->last_status = NPK_REQUEST_ADVANCE_FAIL;
    }
    if (state->last_status == 0) {
        if (outcome == NPK_OUTCOME_REJECTED) {
            state->last_status = NPK_REQUEST_REJECTED;
        } else if (outcome == NPK_OUTCOME_FAILED) {
            state->last_status = NPK_REQUEST_FAILED;
        } else if (outcome != NPK_OUTCOME_COMPLETE) {
            state->last_status = NPK_REQUEST_BAD_OUTCOME;
        }
    }
    response->status = state->last_status;
    if (outcome == NPK_OUTCOME_COMPLETE || outcome == NPK_OUTCOME_REJECTED ||
            outcome == NPK_OUTCOME_FAILED) {
        npk_core_release(slot);
    }
    return (int)state->last_status;
}
