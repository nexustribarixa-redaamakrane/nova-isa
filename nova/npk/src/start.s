; NPK Milestone 1 boot entry.
;
; The reference loader supplies no stack and no firmware services.  _npk_start
; establishes the kernel stack, installs the exception vector from a
; link-computed address, writes the early-boot handshake word, calls the C
; entry point, and halts with its return value in a0.
;
; This is the boot entry only. The machine-mode exception vector is defined in
; `trap.s`. Kernel services live in their own modules; no user loader is
; provided.

.section .text
.global _npk_start
_npk_start:
    LI sp, 0x80000
    LA r1, _npk_exception_vector
    CSRW vbar, r1
    LI r1, 0x90000
    LI r2, 0x4E504B31
    STD r2, [r1]

    ; Publish the typed memory request entry address for post-boot tests. This
    ; is an observation word only; it admits no operations during boot.
    LA r1, npk_memory_request
    LI r2, 0x90F00
    STD r1, [r2]

    ; Publish the typed object request entry address for post-boot tests. This
    ; is an observation word only; it admits no operations during boot.
    LA r1, npk_object_request
    LI r2, 0x96050
    STD r1, [r2]

    ; Publish the typed device/timer request entry address for post-boot tests.
    LA r1, npk_device_request
    LI r2, 0xA0F00
    STD r1, [r2]

    ; Publish the typed task request entry address for post-boot tests.
    LA r1, npk_task_submit_request
    LI r2, 0xB0000
    STD r1, [r2]

    LA ra, .Lnpk_halt
    BL npk_main
.Lnpk_halt:
    HALT
