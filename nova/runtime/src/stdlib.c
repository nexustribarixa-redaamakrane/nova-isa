/* Freestanding libnova integer conversion and process termination. */

#include <limits.h>
#include <stdlib.h>
#include <nova.h>

static int nova_is_space(int c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\v' || c == '\f'
        || c == '\r';
}

static int nova_digit_value(int c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'z') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'Z') {
        return c - 'A' + 10;
    }
    return 99;
}

unsigned long strtoul(const char *text, char **endptr, int base)
{
    const char *s = text;
    unsigned long value = 0;
    int negative = 0;
    int any = 0;
    int overflow = 0;

    while (nova_is_space((unsigned char)*s)) {
        s++;
    }
    if (*s == '+' || *s == '-') {
        negative = (*s == '-');
        s++;
    }

    if (base == 0) {
        if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            base = 16;
            s += 2;
        } else if (s[0] == '0') {
            base = 8;
        } else {
            base = 10;
        }
    } else if (base == 16 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
    }

    if (base < 2 || base > 36) {
        if (endptr != 0) {
            *endptr = (char *)text;
        }
        return 0;
    }

    for (;;) {
        int digit = nova_digit_value((unsigned char)*s);
        if (digit >= base) {
            break;
        }
        any = 1;
        if (value > (ULONG_MAX - (unsigned long)digit) / (unsigned long)base) {
            overflow = 1;
        } else {
            value = value * (unsigned long)base + (unsigned long)digit;
        }
        s++;
    }

    if (endptr != 0) {
        *endptr = (char *)(any ? s : text);
    }
    if (!any) {
        return 0;
    }
    if (overflow) {
        return ULONG_MAX;
    }
    if (negative) {
        return 0UL - value;
    }
    return value;
}

long strtol(const char *text, char **endptr, int base)
{
    const char *s = text;
    unsigned long value = 0;
    unsigned long limit;
    int negative = 0;
    int any = 0;
    int overflow = 0;

    while (nova_is_space((unsigned char)*s)) {
        s++;
    }
    if (*s == '+' || *s == '-') {
        negative = (*s == '-');
        s++;
    }

    if (base == 0) {
        if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            base = 16;
            s += 2;
        } else if (s[0] == '0') {
            base = 8;
        } else {
            base = 10;
        }
    } else if (base == 16 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
    }

    if (base < 2 || base > 36) {
        if (endptr != 0) {
            *endptr = (char *)text;
        }
        return 0;
    }

    limit = negative ? (unsigned long)LONG_MAX + 1UL : (unsigned long)LONG_MAX;
    for (;;) {
        int digit = nova_digit_value((unsigned char)*s);
        if (digit >= base) {
            break;
        }
        any = 1;
        if (value > (limit - (unsigned long)digit) / (unsigned long)base) {
            overflow = 1;
        } else {
            value = value * (unsigned long)base + (unsigned long)digit;
        }
        s++;
    }

    if (endptr != 0) {
        *endptr = (char *)(any ? s : text);
    }
    if (!any) {
        return 0;
    }
    if (overflow) {
        return negative ? LONG_MIN : LONG_MAX;
    }
    if (negative) {
        return (long)(0UL - value);
    }
    return (long)value;
}

int atoi(const char *text)
{
    return (int)strtol(text, 0, 10);
}

void exit(int status)
{
    nova_exit(status);
    for (;;) {
    }
}

void abort(void)
{
    nova_halt(134);
    for (;;) {
    }
}
