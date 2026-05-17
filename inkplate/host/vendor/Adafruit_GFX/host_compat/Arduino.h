#pragma once

// Host-build shim for Arduino.h. Provides the minimum Adafruit_GFX.h
// and Adafruit_GFX.cpp need to compile when there is no Arduino core.
// Selected onto the include path ahead of any system Arduino.h by
// inkplate/host/Makefile (-Ivendor/Adafruit_GFX/host_compat first).

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cmath>

#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*(const unsigned char *)(addr))
#endif
#ifndef pgm_read_word
#define pgm_read_word(addr) (*(const unsigned short *)(addr))
#endif
#ifndef pgm_read_dword
#define pgm_read_dword(addr) (*(const unsigned long *)(addr))
#endif

typedef bool boolean;
typedef uint8_t byte;

class __FlashStringHelper;

#ifndef radians
#define radians(deg) ((deg) * (M_PI / 180.0))
#endif
#ifndef degrees
#define degrees(rad) ((rad) * (180.0 / M_PI))
#endif
#ifndef sq
#define sq(x) ((x) * (x))
#endif
#ifndef abs
#define abs(x) ((x) < 0 ? -(x) : (x))
#endif
