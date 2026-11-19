#ifndef _NOVA_STDIO_H
#define _NOVA_STDIO_H

#ifndef EOF
#define EOF (-1)
#endif

/* Scoped formatter: %c %s %d %i %u %x %X %p %%; no flags/width/precision. */
int printf(const char *format, ...);

int putchar(int character);
int puts(const char *text);

#endif
