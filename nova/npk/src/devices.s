; NPK Milestone 7 CSR accessors.
;
; The freestanding Phase 1 C subset has no inline assembly. These tiny leaves
; expose only the architectural CSRs the device backend is allowed to use:
; `time`, `timecmp`, `ie`, `ip`, `ivec`, and `vbar`. They add no I/O instruction
; and no device or interrupt-controller model.
;
; The nova-cc C ABI returns scalars in r7. a0 is mirrored as the architectural
; scalar slot for NPK assembly helpers; both hold the same sampled value.

.section .text
.global npk_device_read_time
.global npk_device_read_timecmp
.global npk_device_write_timecmp
.global npk_device_read_ie
.global npk_device_write_ie
.global npk_device_read_ip
.global npk_device_write_ip
.global npk_device_read_ivec
.global npk_device_write_ivec
.global npk_device_read_vbar

npk_device_read_time:
    CSRR a0, time
    MOV r7, a0
    RET

npk_device_read_timecmp:
    CSRR a0, timecmp
    MOV r7, a0
    RET

npk_device_write_timecmp:
    CSRW timecmp, r1
    RET

npk_device_read_ie:
    CSRR a0, ie
    MOV r7, a0
    RET

npk_device_write_ie:
    CSRW ie, r1
    RET

npk_device_read_ip:
    CSRR a0, ip
    MOV r7, a0
    RET

npk_device_write_ip:
    CSRW ip, r1
    RET

npk_device_read_ivec:
    CSRR a0, ivec
    MOV r7, a0
    RET

npk_device_write_ivec:
    CSRW ivec, r1
    RET

npk_device_read_vbar:
    CSRR a0, vbar
    MOV r7, a0
    RET
