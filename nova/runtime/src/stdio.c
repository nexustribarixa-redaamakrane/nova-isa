/* Freestanding libnova formatted output (scoped printf subset). */

#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <nova.h>

static int nova_write_all(const char *text, unsigned long count)
{
    unsigned long done = 0;

    while (done < count) {
        long written = nova_write(1, text + done, count - done);
        if (written <= 0) {
            return -1;
        }
        done += (unsigned long)written;
    }
    return (int)done;
}

static int nova_format_unsigned(char *out, unsigned long value,
                                unsigned long base, int upper)
{
    const char *digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    char temp[32];
    int count = 0;
    int position = 0;

    if (value == 0) {
        temp[count++] = '0';
    }
    while (value != 0) {
        temp[count++] = digits[value % base];
        value /= base;
    }
    while (count > 0) {
        count--;
        out[position++] = temp[count];
    }
    return position;
}

static int nova_format_signed(char *out, long value)
{
    char temp[32];
    unsigned long magnitude;
    int count = 0;
    int position = 0;
    int negative = value < 0;

    if (negative) {
        magnitude = (unsigned long)(-(value + 1)) + 1UL;
    } else {
        magnitude = (unsigned long)value;
    }
    if (magnitude == 0) {
        temp[count++] = '0';
    }
    while (magnitude != 0) {
        temp[count++] = (char)('0' + (int)(magnitude % 10UL));
        magnitude /= 10UL;
    }
    if (negative) {
        out[position++] = '-';
    }
    while (count > 0) {
        count--;
        out[position++] = temp[count];
    }
    return position;
}

int printf(const char *format, ...)
{
    va_list ap;
    const char *p;
    int total = 0;

    if (format == 0) {
        return -1;
    }
    va_start(ap, format);
    p = format;
    while (*p != '\0') {
        const char *literal = p;
        int literal_length;
        char buffer[48];
        int converted;
        int direct_write = 0;

        while (*p != '\0' && *p != '%') {
            p++;
        }
        literal_length = (int)(p - literal);
        if (literal_length > 0) {
            if (nova_write_all(literal, (unsigned long)literal_length) < 0) {
                va_end(ap);
                return -1;
            }
            total += literal_length;
        }
        if (*p == '\0') {
            break;
        }

        p++;
        if (*p == '\0') {
            if (nova_write_all("%", 1) < 0) {
                va_end(ap);
                return -1;
            }
            total += 1;
            break;
        }

        converted = 0;
        switch (*p) {
        case '%':
            buffer[0] = '%';
            converted = 1;
            break;
        case 'c': {
            int value = va_arg(ap, int);
            buffer[0] = (char)value;
            converted = 1;
            break;
        }
        case 's': {
            const char *text = va_arg(ap, const char *);
            if (text == 0) {
                text = "(null)";
            }
            converted = nova_write_all(text, strlen(text));
            if (converted < 0) {
                va_end(ap);
                return -1;
            }
            direct_write = 1;
            break;
        }
        case 'd':
        case 'i': {
            int value = va_arg(ap, int);
            converted = nova_format_signed(buffer, (long)value);
            break;
        }
        case 'u': {
            unsigned int value = va_arg(ap, unsigned int);
            converted = nova_format_unsigned(buffer, (unsigned long)value,
                                             10UL, 0);
            break;
        }
        case 'x': {
            unsigned int value = va_arg(ap, unsigned int);
            converted = nova_format_unsigned(buffer, (unsigned long)value,
                                             16UL, 0);
            break;
        }
        case 'X': {
            unsigned int value = va_arg(ap, unsigned int);
            converted = nova_format_unsigned(buffer, (unsigned long)value,
                                             16UL, 1);
            break;
        }
        case 'p': {
            void *pointer = va_arg(ap, void *);
            buffer[0] = '0';
            buffer[1] = 'x';
            converted = 2 + nova_format_unsigned(
                buffer + 2, (unsigned long)pointer, 16UL, 0);
            break;
        }
        default:
            buffer[0] = '%';
            buffer[1] = *p;
            converted = 2;
            break;
        }

        if (converted > 0) {
            if (!direct_write) {
                if (nova_write_all(buffer, (unsigned long)converted) < 0) {
                    va_end(ap);
                    return -1;
                }
            }
            total += converted;
        }
        p++;
    }
    va_end(ap);
    return total;
}

int putchar(int character)
{
    char byte = (char)character;

    if (nova_write_all(&byte, 1) < 0) {
        return EOF;
    }
    return (unsigned char)byte;
}

int puts(const char *text)
{
    if (text == 0) {
        text = "(null)";
    }
    if (nova_write_all(text, strlen(text)) < 0) {
        return EOF;
    }
    if (nova_write_all("\n", 1) < 0) {
        return EOF;
    }
    return 0;
}
