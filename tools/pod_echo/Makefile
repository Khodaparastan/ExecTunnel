# Cross-compile the pod_echo helper for linux/amd64 + linux/arm64.
#
# Output lands in ../../exectunnel/payload/pod_echo/ so the binaries ship
# as package resources and are loadable via
# exectunnel.session._payload.load_pod_echo_b64(arch).

OUT_DIR := ../../exectunnel/payload/pod_echo
GO      ?= go
LDFLAGS := -s -w
BUILD   := CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)'

.PHONY: all amd64 arm64 clean

all: amd64 arm64

$(OUT_DIR):
	mkdir -p $(OUT_DIR)

amd64: | $(OUT_DIR)
	GOOS=linux GOARCH=amd64 $(BUILD) -o $(OUT_DIR)/pod_echo_linux_amd64 .

arm64: | $(OUT_DIR)
	GOOS=linux GOARCH=arm64 $(BUILD) -o $(OUT_DIR)/pod_echo_linux_arm64 .

clean:
	rm -f $(OUT_DIR)/pod_echo_linux_amd64 $(OUT_DIR)/pod_echo_linux_arm64
