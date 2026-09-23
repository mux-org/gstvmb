NAME = gstvmb
FILENAME = $(NAME).tar
PLATFORM = linux/amd64 # note this container is always built for linux/amd64
VIMBAX = VimbaX_Setup-2026-1-Linux64.tar.gz
LIBGSTVMBSRC = libgstvmbsrc.so
CAMSIM_XML ?= VimbaCameraSimulatorTL.xml

# The layer cache is ON by default, so an app-only change rebuilds just the
# `COPY app app` layer — seconds instead of ~10 minutes.
#
# `--no-cache` is not merely slow, it is a hazard here: it re-pulls the floating
# `ubuntu:24.04` tag, so every build runs against a different OS, while
# libgstvmbsrc.so and VimbaX's GenICam libraries are PREBUILT binaries in the
# build context that cannot move with it. A base-image shift under those is
# exactly the kind of fault that presents as a mystery crash at runtime rather
# than a build error. Reach for it deliberately — when apt packages or the
# VimbaX tarball actually change — not by habit:
#
#     make prod NO_CACHE=1
#
NO_CACHE ?=

# `--target` is explicit rather than relying on `prod` being the Dockerfile's
# last stage, so reordering the stages cannot silently ship the dev image.
BUILD = podman build -f Dockerfile -t $(NAME) \
	--platform $(PLATFORM) \
	--build-arg VIMBAX_TAR=$(VIMBAX) \
	--build-arg LIBGSTVMBSRC=$(LIBGSTVMBSRC) \
	$(if $(CAMSIM_XML),--build-arg CAMSIM_XML=$(CAMSIM_XML)) \
	$(if $(NO_CACHE),--no-cache)

prod:
	$(BUILD) --target prod .

dev:
	$(BUILD) --target dev .

save:
	podman save $(NAME) -o $(FILENAME)


.PHONY: prod dev save
.SILENT: prod dev save
