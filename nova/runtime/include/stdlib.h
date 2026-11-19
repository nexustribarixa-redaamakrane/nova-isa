#ifndef _NOVA_STDLIB_H
#define _NOVA_STDLIB_H

#include <stddef.h>

#define EXIT_SUCCESS 0
#define EXIT_FAILURE 1

int atoi(const char *text);
long strtol(const char *text, char **endptr, int base);
unsigned long strtoul(const char *text, char **endptr, int base);

void exit(int status);
void abort(void);

#endif
