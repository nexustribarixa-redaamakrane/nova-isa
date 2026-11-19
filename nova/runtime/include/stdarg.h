#ifndef _NOVA_STDARG_H
#define _NOVA_STDARG_H

/*
 * Nova C ABI v1.0 variadic state: a 24-byte, 8-byte-aligned structure.
 *
 *   offset 0   unsigned int  gp_offset            (bytes consumed in a0-a5)
 *   offset 4   unsigned int  reserved             (zero in this ABI)
 *   offset 8   unsigned long overflow_arg_area    (first stack argument)
 *   offset 16  unsigned long reg_save_area        (48-byte a0-a5 spill area)
 *
 * The compiler implements the four intrinsics below.  va_arg needs a type
 * operand, so the parser recognises __builtin_va_arg specially.
 */
typedef struct __nova_va_list {
    unsigned int __gp_offset;
    unsigned int __fp_offset;
    unsigned long __overflow_arg_area;
    unsigned long __reg_save_area;
} va_list;

void __builtin_va_start(va_list *__ap, ...);
void __builtin_va_end(va_list *__ap);
void __builtin_va_copy(va_list *__dst, va_list *__src);

#define va_start(ap, last) __builtin_va_start(&(ap), last)
#define va_arg(ap, type) __builtin_va_arg(&(ap), type)
#define va_end(ap) __builtin_va_end(&(ap))
#define va_copy(dst, src) __builtin_va_copy(&(dst), &(src))

#endif
