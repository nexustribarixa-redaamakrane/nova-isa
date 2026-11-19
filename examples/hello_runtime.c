#include <stdio.h>
#include <string.h>
#include <stdlib.h>

int main(void)
{
    char buffer[32];
    strcpy(buffer, "world");
    printf("hello %s %d %u %x %X %c %%\n", buffer, -42, 42u, 255u, 255u, '!');
    puts("done");
    return 0;
}
