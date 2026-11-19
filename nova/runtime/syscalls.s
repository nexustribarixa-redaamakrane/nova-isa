; Thin wrappers over the Nova syscall ABI (nova/syscalls.py).
;
; SCALL raises the SYSTEM_CALL exception.  The reference emulator does not
; install a kernel vector; a host model dispatches the call and resumes at
; EPC.  These wrappers introduce no new syscall numbers or ISA surface.

.section .text

.global nova_write
.global write
nova_write:
write:
    SCALL 1
    RET

.global nova_read
.global read
nova_read:
read:
    SCALL 2
    RET

.global nova_time
nova_time:
    SCALL 3
    RET

.global nova_exit
nova_exit:
    SCALL 0
    RET

.global nova_halt
nova_halt:
    HALT
