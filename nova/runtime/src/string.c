/* Freestanding libnova memory and string routines. */

#include <stddef.h>
#include <string.h>

void *memcpy(void *dest, const void *src, size_t count)
{
    unsigned char *d = (unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    size_t i;

    for (i = 0; i < count; i++) {
        d[i] = s[i];
    }
    return dest;
}

void *memmove(void *dest, const void *src, size_t count)
{
    unsigned char *d = (unsigned char *)dest;
    const unsigned char *s = (const unsigned char *)src;
    size_t i;

    if (d == s || count == 0) {
        return dest;
    }
    if (d < s) {
        for (i = 0; i < count; i++) {
            d[i] = s[i];
        }
    } else {
        i = count;
        while (i != 0) {
            i--;
            d[i] = s[i];
        }
    }
    return dest;
}

void *memset(void *dest, int value, size_t count)
{
    unsigned char *d = (unsigned char *)dest;
    unsigned char byte = (unsigned char)value;
    size_t i;

    for (i = 0; i < count; i++) {
        d[i] = byte;
    }
    return dest;
}

int memcmp(const void *left, const void *right, size_t count)
{
    const unsigned char *a = (const unsigned char *)left;
    const unsigned char *b = (const unsigned char *)right;
    size_t i;

    for (i = 0; i < count; i++) {
        if (a[i] != b[i]) {
            return (int)a[i] - (int)b[i];
        }
    }
    return 0;
}

size_t strlen(const char *text)
{
    const char *p = text;

    while (*p != '\0') {
        p++;
    }
    return (size_t)(p - text);
}

int strcmp(const char *left, const char *right)
{
    const unsigned char *a = (const unsigned char *)left;
    const unsigned char *b = (const unsigned char *)right;

    while (*a != '\0' && *a == *b) {
        a++;
        b++;
    }
    return (int)*a - (int)*b;
}

int strncmp(const char *left, const char *right, size_t count)
{
    const unsigned char *a = (const unsigned char *)left;
    const unsigned char *b = (const unsigned char *)right;
    size_t i;

    for (i = 0; i < count; i++) {
        if (a[i] != b[i]) {
            return (int)a[i] - (int)b[i];
        }
        if (a[i] == '\0') {
            return 0;
        }
    }
    return 0;
}

char *strcpy(char *dest, const char *src)
{
    char *d = dest;
    const char *s = src;

    while ((*d++ = *s++) != '\0') {
    }
    return dest;
}

char *strncpy(char *dest, const char *src, size_t count)
{
    size_t i = 0;
    char *d = dest;
    const char *s = src;

    while (i < count && s[i] != '\0') {
        d[i] = s[i];
        i++;
    }
    while (i < count) {
        d[i] = '\0';
        i++;
    }
    return dest;
}

char *strcat(char *dest, const char *src)
{
    char *d = dest;

    while (*d != '\0') {
        d++;
    }
    strcpy(d, src);
    return dest;
}
