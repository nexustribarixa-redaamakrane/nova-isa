#ifndef _NOVA_NOVA_H
#define _NOVA_NOVA_H

#include <stddef.h>

/*
 * Thin wrappers over the Nova syscall ABI (nova/syscalls.py).
 *
 * The reference emulator does not install a kernel; a host dispatcher must
 * call nova.syscalls.dispatch_syscall after a SYSTEM_CALL exception.  SYS_READ
 * has no standard reference-model input source and returns -1/unsupported.
 */
long nova_write(int fd, const void *buffer, unsigned long count);
long nova_read(int fd, void *buffer, unsigned long count);
long nova_time(void);
void nova_exit(int status);

/* Conventional names for freestanding Nova programs. */
long write(int fd, const void *buffer, unsigned long count);
long read(int fd, void *buffer, unsigned long count);

/* Architectural HALT, leaving `status` in a0 for the reference model. */
void nova_halt(int status);

#endif
