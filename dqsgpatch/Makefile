THEOS=/opt/theos1
THEOS_PACKAGE_SCHEME = rootless
SDKVERSION = 16.5
SYSROOT = $(shell xcrun --sdk iphoneos --show-sdk-path)

TARGET := iphone:clang:latest:14.0
ARCHS = arm64

INSTALL_TARGET_PROCESSES = DQSG

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = dqsgpatch

dqsgpatch_FILES = Tweak.x
dqsgpatch_CFLAGS = -fobjc-arc -Wno-deprecated-declarations
dqsgpatch_FRAMEWORKS = Foundation UIKit
dqsgpatch_PRIVATE_FRAMEWORKS =

include $(THEOS_MAKE_PATH)/tweak.mk
