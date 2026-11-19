; Nova64 freestanding startup.
;
; The reference emulator supplies no stack and no register state.  _start is
; the linker entry symbol: it establishes a 16-byte-aligned initial stack,
; calls the C entry point main() with no arguments, and halts with main's
; return value in a0.  HALT is the architectural termination mechanism here;
; SYS_EXIT remains available through nova_exit() when a syscall dispatcher is
; present.  This is not hosted operating-system startup.

.section .text
.global _start
_start:
    LI sp, 0x80000
    LA ra, .Lnova_start_return
    BL main
.Lnova_start_return:
    HALT
