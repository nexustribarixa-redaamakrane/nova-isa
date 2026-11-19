; NPK Milestone 1 boot entry.
;
; The reference loader supplies no stack and no firmware services.  _npk_start
; establishes the kernel stack, installs the exception vector from a
; link-computed address, writes the early-boot handshake word, calls the C
; entry point, and halts with its return value in a0.
;
; This is a boot foundation only: `npk_main` runs the minimal boot-verification
; processing pipeline, but there is no trap dispatcher, allocator, syscall
; handler, device driver, scheduler, or user loader yet.

.section .text
.global _npk_start
_npk_start:
    LI sp, 0x80000
    LA r1, _npk_exception_vector
    CSRW vbar, r1
    LI r1, 0x90000
    LI r2, 0x4E504B31
    STD r2, [r1]
    LA ra, .Lnpk_halt
    BL npk_main
.Lnpk_halt:
    HALT

.global _npk_exception_vector
_npk_exception_vector:
    HALT
