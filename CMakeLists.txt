cmake_minimum_required(VERSION 3.20)
project(nova_npk_kernel NONE)

# CMake orchestrates the existing Nova toolchain; it does not compile NPK with
# a host C compiler and does not introduce a second kernel build path.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_program(QEMU_EXECUTABLE
    NAMES qemu-system-x86_64 qemu-system-x86_64.exe
    PATHS "C:/Program Files/qemu" "C:/Program Files/QEMU"
)

set(NPK_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}")
set(NPK_KERNEL_DIR "${CMAKE_CURRENT_BINARY_DIR}/npk-kernel"
    CACHE PATH "Output directory for the NPK kernel image")
set(NPK_KERNEL_IMAGE "${NPK_KERNEL_DIR}/npk.nob")
option(NPK_ENABLE_QEMU "Add the QEMU boot-validation target" ON)

file(GLOB_RECURSE NPK_BUILD_INPUTS CONFIGURE_DEPENDS LIST_DIRECTORIES false
    "${NPK_SOURCE_ROOT}/nova/*.py"
    "${NPK_SOURCE_ROOT}/nova/runtime/include/*.h"
    "${NPK_SOURCE_ROOT}/nova/npk/src/*.c"
    "${NPK_SOURCE_ROOT}/nova/npk/src/*.s"
)
list(APPEND NPK_BUILD_INPUTS
    "${NPK_SOURCE_ROOT}/scripts/npk_cmake_build.py"
    "${NPK_SOURCE_ROOT}/pyproject.toml"
)

add_custom_command(
    OUTPUT "${NPK_KERNEL_IMAGE}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${NPK_KERNEL_DIR}"
    COMMAND "${Python3_EXECUTABLE}"
            "${NPK_SOURCE_ROOT}/scripts/npk_cmake_build.py"
            --output-dir "${NPK_KERNEL_DIR}"
    DEPENDS ${NPK_BUILD_INPUTS}
    WORKING_DIRECTORY "${NPK_SOURCE_ROOT}"
    COMMENT "Building npk.nob with the Nova assembler, compiler, and linker"
    VERBATIM
)

add_custom_target(npk_kernel ALL DEPENDS "${NPK_KERNEL_IMAGE}")

add_custom_target(npk_validate
    COMMAND "${Python3_EXECUTABLE}" "scripts/npk_validate.py"
    WORKING_DIRECTORY "${NPK_SOURCE_ROOT}"
    COMMENT "Running the NPK and Phase 1 regression suites"
    VERBATIM
)

if(NPK_ENABLE_QEMU)
    set(NPK_QEMU_ARGS
        --kernel "${NPK_KERNEL_IMAGE}"
        --verbose
    )
    if(QEMU_EXECUTABLE)
        list(APPEND NPK_QEMU_ARGS --qemu "${QEMU_EXECUTABLE}")
    endif()

    add_custom_target(npk_qemu_boot
        COMMAND "${Python3_EXECUTABLE}"
                "scripts/npk_qemu_boot.py" ${NPK_QEMU_ARGS}
        DEPENDS "${NPK_KERNEL_IMAGE}"
        WORKING_DIRECTORY "${NPK_SOURCE_ROOT}"
        COMMENT "Booting npk.nob in QEMU through the generated Nova interpreter"
        VERBATIM
    )

    if(QEMU_EXECUTABLE)
        message(STATUS "NPK QEMU validator: ${QEMU_EXECUTABLE}")
    else()
        message(STATUS "NPK QEMU validator: qemu-system-x86_64 not found; "
                       "npk_qemu_boot will use PATH")
    endif()
endif()
