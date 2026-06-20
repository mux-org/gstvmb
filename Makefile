NAME = gstvmb
FILENAME = $(NAME).tar
PLATFORM = linux/amd64 # note this container is always built for linux/amd64
VIMBAX = VimbaX_Setup-2026-1-Linux64.tar.gz
LIBGSTVMBSRC = libgstvmbsrc.so
CAMSIM_XML ?= VimbaCameraSimulatorTL.xml

prod:
	podman build -f Dockerfile -t $(NAME) \
		--platform $(PLATFORM) \
		--build-arg VIMBAX_TAR=$(VIMBAX) \
		--build-arg LIBGSTVMBSRC=$(LIBGSTVMBSRC) \
		$(if $(CAMSIM_XML),--build-arg CAMSIM_XML=$(CAMSIM_XML)) \
		--no-cache .

dev:
	podman build -f Dockerfile -t $(NAME) \
		--target dev \
		--platform $(PLATFORM) \
		--build-arg VIMBAX_TAR=$(VIMBAX) \
		--build-arg LIBGSTVMBSRC=$(LIBGSTVMBSRC) \
		$(if $(CAMSIM_XML),--build-arg CAMSIM_XML=$(CAMSIM_XML)) \
		--no-cache .

save:
	podman save $(NAME) -o $(FILENAME)


.PHONY: prod dev save
.SILENT: prod dev save
