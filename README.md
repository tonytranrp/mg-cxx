# clang-mg / C++MG

**clang-mg** is the compiler executable for **C++MG**, an experimental Clang/LLVM-based C++ compiler project exploring new C++ language features for clearer compile-time programming.

The project asks a practical question:

> What would C++ feel like if common compile-time patterns were supported directly by the compiler?

C++MG focuses on features that make C++ easier to configure, compose, and extend while preserving the performance, control, and systems-level strengths that make C++ valuable.

- Project repository: [github.com/mgorn/mg-cxx](https://github.com/mgorn/mg-cxx)
- Website: [mwg.codes/clang-mg](https://mwg.codes/clang-mg/)
- Community: [Discord](https://discord.gg/RM8BVwAfZy)

---

## Why this exists

Modern C++ is powerful, but advanced compile-time behavior often depends on templates, macros, partial specializations, helper base classes, and other indirect patterns. Those tools work, but they can make code harder to read, modify, generate, and verify.

C++MG provides compiler features that let programmers express intent more directly. The goal is not to replace the strengths of C++, but to make certain common patterns easier to write and easier to reason about.

This matters for humans and for AI-assisted development. AI coding tools can help generate and maintain software, but they can struggle when a language requires scattered template specializations, macro-heavy code, or several indirect versions of the same type. Cleaner compile-time features could make generated C++ easier to inspect while still compiling down to efficient code.

C++MG is especially interested in software that needs compile-time configuration, such as:

- game engines
- embedded systems
- operating systems
- graphics libraries
- cross-platform applications
- WebAssembly modules
- compiler tooling
- performance-sensitive libraries

In these kinds of projects, developers often want unused members, code paths, or dependencies to disappear at compile time instead of relying on runtime checks.

---

## Project status

C++MG is currently experimental. Syntax, implementation details, feature names, diagnostics, and patch layout may change as the project develops.

This project is intended for experiments, prototypes, compiler hacking, and discussion about future C++ language design. It should not be treated as a stable production compiler YET.

---

## Features

### Class-scope `if constexpr`

Standard C++ often forces conditional members through helper types or partial specializations. For example, a conditional field may require an extra storage template:

```cpp
template<bool Enabled>
struct CounterStorage {};

template<>
struct CounterStorage<true> {
  int counter = 0;
};

struct B {
  static constexpr bool hasCounter = true;

  CounterStorage<hasCounter> storage;

  void func() {
    if constexpr (hasCounter) {
      storage.counter = 2;
    }
  }
};
```

With C++MG, the intent can be written directly inside the class body:

```cpp
struct B {
  static constexpr bool hasCounter = true;

  if constexpr (hasCounter) {
    int counter = 0;
  }

  // NOTE: Either 'B' or 'func' must be templates so that the conditional body is not semantically checked immediately
  void func() {
    if constexpr (hasCounter) {
      counter = 2;
    }
  }
};
```

The member only exists when the condition is true.

When using conditional members, code that accesses those members should also be guarded by a compile-time check. Otherwise, the compiler may still semantically analyze code that refers to a member that does not exist for a given type.

Feature detection is available through the `__cxxmg_if_constexpr_member` macro:

```cpp
#ifdef __cxxmg_if_constexpr_member
struct Example {
  static constexpr bool enabled = true;

  if constexpr (enabled) {
    int value = 0;
  }
};
#endif
```

Clang-style feature detection is also available:

```cpp
#if __has_feature(cxxmg_if_constexpr_members)
// C++MG conditional class members are available.
#endif
```

---

### Renamed compiler executable: `clang-mg`

The compiler executable is named `clang-mg` instead of `clang`.

This keeps C++MG separate from a normal Clang installation, making it easier to install, test, and use both compilers on the same system without conflicts.

Example:

```bash
clang-mg++ main.cpp -o app
```

---

### `#urlinclude`

C++MG adds a `#urlinclude` directive for downloading a remote header, caching it locally, and including it like a normal header.

Example:

```cpp
#urlinclude "https://example.com/some/header.hpp"

int main() {
  return 0;
}
```

Downloaded files are cached in `.cxxmg-cache/` so the same header does not need to be downloaded repeatedly. The compiler may use `curl`, `wget`, or a configured downloader.

Both quote and angle forms are supported and use the same cache entry:

```cpp
#urlinclude "https://example.com/some/header.hpp"
#urlinclude <https://example.com/some/header.hpp>
```

Useful options:

- `-furlinclude` / `-fno-urlinclude`: enable or disable the directive.
- `-furlinclude-cache-dir=<path>`: choose a cache directory.
- `-furlinclude-tool=<tool>`: choose `curl`, `wget`, or a custom downloader.
- `-furlinclude-tool-arg=<arg>`: pass an extra argument to a custom downloader.
- `-furlinclude-timeout=<seconds>`: set the download timeout.
- `-furlinclude-offline`: use only cached URL headers.
- `-furlinclude-refresh`: re-download URL headers and update the cache after successful downloads.
- `-furlinclude-progress=auto|always|never`: control downloader progress output.
- `-furlinclude-allow-http`: permit insecure `http://` URLs.

Feature detection is available through the `__cxxmg_urlinclude` macro:

```cpp
#ifdef __cxxmg_urlinclude
#urlinclude "https://example.com/some/header.hpp"
#endif
```

Remote includes are useful for experiments, examples, small projects, and quick dependency tests. For production code, use them carefully: remote includes can create security, reproducibility, and availability risks. Prefer pinned URLs, trusted sources, offline mode, committed lockfiles, or vendored copies when stability matters.

An advised CI pattern is to populate the cache once, then build with:

```bash
clang-mg++ -furlinclude-offline main.cpp -o app
```

---

### Traits

C++MG traits are a lightweight way to describe the structure a type should have. They are intended to be simpler than full C++ concepts when all you need is a structural member check.

A trait looks similar to a `struct` or `class`, but it describes a required shape instead of defining a normal type:

```cpp
struct S {
  int value = 0;
};

trait ValueTrait {
  int value;
};
```

Traits can then be used to test whether a type has the required members:

```cpp
static constexpr bool hasValue = S implements ValueTrait;
```

or use the implements operator `<>` instead:

```cpp
static constexpr bool hasValue = S <> ValueTrait;
```

The goal is to make simple structural checks readable without requiring a more complicated concepts-based setup.

---

### Type expressions

Type expressions allow classes, structs, and traits to be combined or compared more directly. The idea is similar to set operations, but applied to type members.

```cpp
struct A {
  int value = 0;
};

struct B {
  bool test = false;
};

using C = A + B;
```

Conceptually, that produces a type like this:

```cpp
struct C {
  int value = 0;
  bool test = false;
};
```

Traits can also be combined:

```cpp
trait ValueTrait {
  int value;
};

trait TestTrait {
  bool test;
};

using CombinedTrait = ValueTrait + TestTrait;
```

Conceptually, that produces a trait like this:

```cpp
trait CombinedTrait {
  int value;
  bool test;
};
```

You can then check whether a type matches part or all of a trait expression:

```cpp
static constexpr bool hasRequiredMembers = C implements CombinedTrait; // or: C <> CombinedTrait
```

The goal is to make structural composition and structural checks easier to express directly in the language.

---

## Simple example use case

A project may have a struct that needs extra debugging fields only when debug mode is enabled:

```cpp
struct Entity {
  int id;
  float x;
  float y;

  static constexpr bool DebugMode = true;

  if constexpr (DebugMode) {
    const char* debugName;
    int debugFlags;
  }
};
```

With class-scope `if constexpr`, debug-only fields can be written directly inside the struct. When debug mode is disabled, those members do not exist.

This avoids needing a separate debug version of the struct or preprocessor macros around the fields.

---

## Building

Maintaining a full LLVM fork can be difficult because LLVM changes constantly. This repository is organized around patch sets that can be applied to an LLVM checkout to enable individual C++MG features.

A typical workflow is:

```bash
# Clone or update LLVM.
./build.sh update

# Apply the desired C++MG patches.
./build.sh apply <feature>

# Configure and build LLVM/Clang.
./build.sh build

# Install the compiler or add it to PATH.
./build.sh install
```

After installation, use the resulting `clang-mg` executable to compile test programs.

On Windows, use the PowerShell build script:

```powershell
.\build.ps1 build
```

The exact build command may vary depending on your platform, generator, LLVM checkout location, and enabled features.

---

## Testing a feature

After building, create a small test file:

```cpp
struct S {
  static constexpr bool enabled = false;

  if constexpr (enabled) {
    int value = 0;
  }
};

int main() {
  S s;
  return 0;
}
```

Compile it with C++MG:

```bash
clang-mg++ test.cpp -o test
```

To verify that disabled conditional members are not available, this should fail when `enabled` is `false`:

```cpp
struct S {
  static constexpr bool enabled = false;

  if constexpr (enabled) {
    int value = 0;
  }
};

int main() {
  S s;
  s.value = 1; // Expected error: value does not exist when enabled is false.
}
```

---

## Patch-based workflow

Another important part of C++MG is the patch management system.

Rather than keeping every experiment inside one large fork, features are saved as patch sets. This makes it easier to:

- separate experimental features
- apply features one at a time
- update against newer LLVM versions
- track what files were changed
- share or remove features cleanly

This workflow keeps the project more organized and easier to maintain as LLVM changes over time.

---

## Implementation challenges

Clang is a large and complex codebase. A language feature usually affects more than one part of the compiler.

For a feature like class-scope `if constexpr`, changes may be needed in areas such as:

- the parser
- semantic analysis
- AST declaration handling
- template instantiation
- diagnostics
- tests

The feature also needs to behave correctly in more complicated C++ cases, such as templates, access specifiers, member functions, typedefs, using declarations, and static members.

---

## Goals

C++MG is especially interested in features that:

- reduce template boilerplate
- make compile-time configuration easier to read
- improve structural programming in C++
- keep generated code efficient
- help humans and AI tools work with C++ more effectively

The larger goal is to explore how C++ could become more expressive without giving up performance, control, or compatibility with systems programming workflows.

---

## Contributing

Issues, experiments, bug reports, and feature ideas are welcome.

If you test the compiler and find a case where a feature behaves incorrectly, please include:

- the smallest code example that reproduces the issue
- the command used to compile it
- the expected behavior
- the actual behavior
- your operating system and compiler build details

---

## License

Licensing is still being decided. Please get in touch before using C++MG in production.
