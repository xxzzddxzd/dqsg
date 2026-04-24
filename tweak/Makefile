export THEOS=/opt/theos1
export ARCHS = arm64
export TARGET = iphone:clang:latest:15.0

INSTALL_TARGET_PROCESSES = DQSG

include $(THEOS)/makefiles/common.mk

TWEAK_NAME = DQSGApiLogger

DQSGApiLogger_FILES = Tweak.xm
DQSGApiLogger_CFLAGS = -fobjc-arc
DQSGApiLogger_FRAMEWORKS = Foundation

include $(THEOS)/makefiles/tweak.mk
