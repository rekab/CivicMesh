#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>

class String {
public:
    String() = default;
    String(const char* s) : s_(s ? s : "") {}
    size_t length() const { return s_.size(); }
    const char* c_str() const { return s_.c_str(); }

private:
    std::string s_;
};

class Print {
public:
    virtual ~Print() = default;
    virtual size_t write(uint8_t) = 0;
    virtual size_t write(const uint8_t* buf, size_t n) {
        size_t r = 0;
        for (size_t i = 0; i < n; i++) {
            size_t w = write(buf[i]);
            if (w == 0) return r;
            r += w;
        }
        return r;
    }
    size_t write(const char* s) {
        return write(reinterpret_cast<const uint8_t*>(s), std::strlen(s));
    }
    size_t write(const char* s, size_t n) {
        return write(reinterpret_cast<const uint8_t*>(s), n);
    }
    size_t print(const char* s) { return write(s); }
    size_t print(char c) { return write(static_cast<uint8_t>(c)); }
    size_t println() {
        const uint8_t crlf[2] = {'\r', '\n'};
        return write(crlf, 2);
    }
};
