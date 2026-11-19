#ifndef _NOVA_STDDEF_H
#define _NOVA_STDDEF_H

/* Nova64 LP64 freestanding subset. */
typedef unsigned long size_t;
typedef long ptrdiff_t;

#ifndef NULL
#define NULL ((void *)0)
#endif

#define offsetof(type, member) ((size_t)&(((type *)0)->member))

#endif
