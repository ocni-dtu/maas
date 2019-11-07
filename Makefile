python := python3
snapcraft := SNAPCRAFT_BUILD_INFO=1 /snap/bin/snapcraft

VENV := .ve

# pkg_resources makes some incredible noise about version numbers. They
# are not indications of bugs in MAAS so we silence them everywhere.
export PYTHONWARNINGS = \
  ignore:You have iterated over the result:RuntimeWarning:pkg_resources:

# If offline has been selected, attempt to further block HTTP/HTTPS
# activity by setting bogus proxies in the environment.
ifneq ($(offline),)
export http_proxy := broken
export https_proxy := broken
endif

asset_deps := \
  $(shell find src -name '*.js' -not -path '*/maasserver/static/js/bundle/*') \
  $(shell find src -name '*.scss') \
  package.json \
  webpack.config.js \
  yarn.lock

asset_output := \
  src/maasserver/static/css/build.css \
  src/maasserver/static/js/bundle/maas-min.js \
  src/maasserver/static/js/bundle/maas-min.js.map \
  src/maasserver/static/js/bundle/vendor-min.js \
  src/maasserver/static/js/bundle/vendor-min.js.map

# Prefix commands with this when they need access to the database.
# Remember to add a dependency on bin/database from the targets in
# which those commands appear.
dbrun := bin/database --preserve run --

# Path to install local nodejs.
mkfile_dir := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))
nodejs_path := $(mkfile_dir)/include/nodejs/bin

export GOPATH := $(shell go env GOPATH)
export PATH := $(GOPATH)/bin:$(nodejs_path):$(PATH)

# For anything we start, we want to hint as to its root directory.
export MAAS_ROOT := $(CURDIR)/.run
# For things that care, postgresfixture for example, we always want to
# use the "maas" databases.
export PGDATABASE := maas

# Check if a command is found on PATH. Raise an error if not, citing
# the package to install. Return the command otherwise.
# Usage: $(call available,<command>,<package>)
define available
  $(if $(shell which $(1)),$(1),$(error $(1) not found; \
    install it with 'sudo apt install $(2)'))
endef

.DEFAULT_GOAL := build

build: \
  .run \
  $(VENV) \
  bin/database \
  bin/maas \
  bin/maas-common \
  bin/maas-rack \
  bin/maas-region \
  bin/rackd \
  bin/regiond \
  bin/test.cli \
  bin/test.rack \
  bin/test.region \
  bin/test.region.legacy \
  bin/test.testing \
  bin/test.parallel \
  bin/postgresfixture \
  bin/py \
  machine-resources \
  pycharm
.PHONY: build

all: build doc
.PHONY: all

REQUIRED_DEPS_FILES = base build dev doc
FORBIDDEN_DEPS_FILES = forbidden

# list package names from a required-packages/ file
list_required = $(shell sort -u required-packages/$1 | sed '/^\#/d')

# Install all packages required for MAAS development & operation on
# the system. This may prompt for a password.
install-dependencies: release := $(shell lsb_release -c -s)
install-dependencies:
	sudo DEBIAN_FRONTEND=noninteractive apt install --no-install-recommends -y \
		$(foreach deps,$(REQUIRED_DEPS_FILES),$(call list_required,$(deps)))
	sudo DEBIAN_FRONTEND=noninteractive apt purge -y \
		$(foreach deps,$(FORBIDDEN_DEPS_FILES),$(call list_required,$(deps)))
	if [ -x /usr/bin/snap ]; then cat required-packages/snaps | xargs -L1 sudo snap install; fi
.PHONY: install-dependencies

sudoers:
	utilities/install-sudoers
	utilities/grant-nmap-permissions
.PHONY: sudoers

$(VENV): requirements.txt
	python3 -m venv --system-site-packages --clear $@
	$(VENV)/bin/pip install -r requirements.txt

bin/black bin/coverage \
  bin/postgresfixture \
  bin/maas bin/rackd bin/regiond \
  bin/maas-region bin/maas-rack bin/maas-common \
  bin/test.region bin/test.region.legacy \
  bin/test.rack bin/test.cli \
  bin/test.testing bin/test.parallel:
	mkdir -p bin
	ln -sf ../$(VENV)/$@ $@

bin/py:
	ln -sf ../$(VENV)/bin/ipython $@

# bin/node-sass is needed for checking css.
bin/test.testing: bin/node-sass

bin/database: bin/postgresfixture
	ln -sf $(notdir $<) $@

include/nodejs/bin/node:
	mkdir -p include/nodejs
	wget -O include/nodejs/nodejs.tar.gz https://nodejs.org/dist/v8.10.0/node-v8.10.0-linux-x64.tar.gz
	tar -C include/nodejs/ -xf include/nodejs/nodejs.tar.gz --strip-components=1

include/nodejs/yarn.tar.gz:
	mkdir -p include/nodejs
	wget -O include/nodejs/yarn.tar.gz https://yarnpkg.com/latest.tar.gz

include/nodejs/bin/yarn: include/nodejs/yarn.tar.gz
	tar -C include/nodejs/ -xf include/nodejs/yarn.tar.gz --strip-components=1
	@touch --no-create $@

bin/yarn: include/nodejs/bin/yarn
	@mkdir -p bin
	ln -sf ../include/nodejs/bin/yarn $@
	@touch --no-create $@

machine-resources-vendor:
	$(MAKE) -C src/machine-resources vendor
.PHONY: machine-resources-vendor

machine-resources: machine-resources-vendor
	$(MAKE) -C src/machine-resources build
.PHONY: machine-resources

node_modules: include/nodejs/bin/node bin/yarn
	bin/yarn --frozen-lockfile
	@touch --no-create $@

define js_bins
  bin/node-sass
  bin/webpack
endef

$(strip $(js_bins)): node_modules
	ln -sf ../node_modules/.bin/$(notdir $@) $@
	@touch --no-create $@

define node_packages
  @babel/core
  @babel/preset-react
  @babel/preset-es2015
  @types/prop-types
  @types/react
  @types/react-dom
  babel-polyfill
  babel-loader@^8.0.0-beta.0
  glob
  jasmine-core@=2.99.1
  macaroon-bakery
  node-sass
  prop-types
  react
  react-dom
  react2angular
  vanilla-framework
  webpack
  webpack-cli
  webpack-merge
endef

force-yarn-update: bin/yarn
	$(RM) package.json yarn.lock
	bin/yarn add -D $(strip $(node_packages))
.PHONY: force-yarn-update

define test-scripts
  bin/test.cli
  bin/test.rack
  bin/test.region
  bin/test.region.legacy
  bin/test.testing
endef

lxd:
	utilities/configure-lxd-profile
	utilities/create-lxd-bionic-image
.PHONY: lxd

test: test-js test-py
.PHONY: test

test-py: bin/test.parallel bin/coverage
	@$(RM) .coverage .coverage.*
	@bin/test.parallel --with-coverage --subprocess-per-core
	@bin/coverage combine
.PHONY: test-py

test-js: assets
	bin/yarn test
.PHONY: test-js

test-js-watch: assets
	bin/yarn test --watch
.PHONY: test-js-watch

test-serial: $(strip $(test-scripts))
	@bin/maas-region makemigrations --dry-run --exit && exit 1 ||:
	@$(RM) .coverage .coverage.* .failed
	$(foreach test,$^,$(test-template);)
	@test ! -f .failed
.PHONY: test-serial

test-failed: $(strip $(test-scripts))
	@bin/maas-region makemigrations --dry-run --exit && exit 1 ||:
	@$(RM) .coverage .coverage.* .failed
	$(foreach test,$^,$(test-template-failed);)
	@test ! -f .failed
.PHONY: test-failed

clean-failed:
	$(RM) .noseids
.PHONY: clean-failed

src/maasserver/testing/initial.maas_test.sql: bin/maas-region bin/database
    # Run migrations without any triggers created.
	$(dbrun) bin/maas-region dbupgrade --internal-no-triggers
    # Data migration will create a notification, that will break tests. Want
    # the database to be a clean schema.
	$(dbrun) bin/maas-region shell -c "from maasserver.models.notification import Notification; Notification.objects.all().delete()"
	$(dbrun) pg_dump maas --no-owner --no-privileges --format=plain > $@

test-initial-data: src/maasserver/testing/initial.maas_test.sql
.PHONY: test-initial-data

define test-template
$(test) --with-xunit --xunit-file=xunit.$(notdir $(test)).xml || touch .failed
endef

define test-template-failed
  $(test) --with-xunit --xunit-file=xunit.$(notdir $(test)).xml --failed || \
  $(test) --with-xunit --xunit-file=xunit.$(notdir $(test)).xml --failed || \
  touch .failed
endef

smoke: lint bin/maas-region bin/test.rack
	@bin/maas-region makemigrations --dry-run --exit && exit 1 ||:
	@bin/test.rack --stop
.PHONY: smoke

test-serial+coverage: export NOSE_WITH_COVERAGE = 1
test-serial+coverage: test-serial
.PHONY: test-serial-coverage

coverage-report: coverage/index.html
	sensible-browser $< > /dev/null 2>&1 &
.PHONY: coverage-report

coverage.xml: bin/coverage .coverage
	bin/coverage xml -o $@

coverage/index.html: revno = $(or $(shell git rev-parse HEAD 2>/dev/null),???)
coverage/index.html: bin/coverage .coverage
	@$(RM) -r $(@D)
	bin/coverage html \
	    --title "Coverage for MAAS rev $(revno)" \
	    --directory $(@D)

.coverage:
	@$(error Use `$(MAKE) test` to generate coverage)

lint: lint-py lint-py-imports lint-py-linefeeds lint-js lint-go
.PHONY: lint

pocketlint = $(call available,pocketlint,python-pocket-lint)

# XXX jtv 2014-02-25: Clean up this lint, then make it part of "make lint".
lint-css: sources = src/maasserver/static/css
lint-css:
	@find $(sources) -type f \
	    -print0 | xargs -r0 $(pocketlint) --max-length=120
.PHONY: lint-css

lint-py: sources = $(wildcard *.py contrib/*.py) src utilities etc
lint-py: bin/black
	@bin/black $(sources) --check
.PHONY: lint-py

# Statically check imports against policy.
lint-py-imports: sources = setup.py src
lint-py-imports:
	@utilities/check-imports
	@find $(sources) -type f -name '*.py' \
	  ! -path '*/migrations/*' \
	  -print0 | xargs -r0 utilities/find-early-imports
.PHONY: lint-py-imports

# Only Unix line ends should be accepted
lint-py-linefeeds:
	@find src/ -name \*.py -exec file "{}" ";" | \
	    awk '/CRLF/ { print $0; count++ } END {exit count}' || \
	    (echo "Lint check failed; run make format to fix DOS linefeeds."; false)
.PHONY: lint-py-linefeeds

# JavaScript lint is checked in parallel for speed.  The -n20 -P4 setting
# worked well on a multicore SSD machine with the files cached, roughly
# doubling the speed, but it may need tuning for slower systems or cold caches.
lint-js: sources = src/maasserver/static/js
lint-js:
	@find $(sources) -type f -not -path '*/angular/3rdparty/*' -a \
		-not -path '*-min.js' -a \
	    '(' -name '*.html' -o -name '*.js' ')' -print0 \
		| xargs -r0 -n20 -P4 $(pocketlint)
		bin/yarn lint
		bin/yarn prettier-check
.PHONY: lint-js

# Go fmt
lint-go:
	@find src/ \( -name pkg -o -name vendor \) -prune -o -name '*.go' -exec gofmt -l {} + | \
		tee /tmp/gofmt.lint
	@test ! -s /tmp/gofmt.lint
.PHONY: lint-go

format.parallel:
	@$(MAKE) -s -j format
.PHONY: format.parallel

# Apply automated formatting to all Python, Sass and Javascript files.
format: format-python format-js format-go
.PHONY: format

format-python: sources = $(wildcard *.py contrib/*.py) src utilities etc
format-python: bin/black
	@bin/black -q $(sources)
.PHONY: format-python

format-js: bin/yarn
	@bin/yarn -s prettier --loglevel warn
.PHONY: format-js

format-go:
	@find src/ -name '*.go' -execdir go fmt {} +
.PHONY: format-go

check: clean test
.PHONY: check

api-docs.rst: bin/maas-region src/maasserver/api/doc_handler.py syncdb
	bin/maas-region generate_api_doc > $@

sampledata: bin/maas-region bin/database syncdb
	$(dbrun) bin/maas-region generate_sample_data
.PHONY: sampledata

doc: api-docs.rst
.PHONY: doc

.run: run-skel
	@cp --archive --verbose $^ $@

.idea: contrib/pycharm
	@cp --archive --verbose $^ $@

pycharm: .idea
.PHONY: pycharm

assets: node_modules $(asset_output)
.PHONY: assets

force-assets: clean-assets node_modules $(asset_output)
.PHONY: force-assets

lander-javascript: force-assets
	git update-index -q --no-assume-unchanged $(strip $(asset_output)) 2> /dev/null || true
	git add -f $(strip $(asset_output)) 2> /dev/null || true
.PHONY: lander-javascript

lander-styles: lander-javascript
.PHONY: lander-styles

# The $(subst ...) uses a pattern rule to ensure Webpack runs just once,
# even if all four output files are out-of-date.
$(subst .,%,$(asset_output)): node_modules $(asset_deps)
	bin/yarn build
	@touch --no-create $(strip $(asset_output))
	@git update-index -q --assume-unchanged $(strip $(asset_output)) 2> /dev/null || true

clean-assets:
	$(RM) -r src/maasserver/static/js/bundle
	$(RM)  -r src/maasserver/static/css
.PHONY: clean-assets

watch-assets:
	bin/yarn watch
.PHONY: watch-assets

clean: stop clean-failed clean-assets
	find . -type f -name '*.py[co]' -print0 | xargs -r0 $(RM)
	find . -type d -name '__pycache__' -print0 | xargs -r0 $(RM) -r
	find . -type f -name '*~' -print0 | xargs -r0 $(RM)
	$(RM) -r media/demo/* media/development media/development.*
	$(RM) src/maasserver/data/templates.py
	$(RM) *.log
	$(RM) api-docs.rst
	$(RM) .coverage .coverage.* coverage.xml
	$(RM) -r coverage
	$(RM) -r .hypothesis
	$(RM) -r bin include lib local node_modules
	$(RM) -r eggs develop-eggs
	$(RM) -r build dist logs/* parts
	$(RM) tags TAGS .installed.cfg
	$(RM) -r *.egg *.egg-info src/*.egg-info
	$(RM) -r services/*/supervise
	$(RM) -r .run
	$(RM) -r .idea
	$(RM) xunit.*.xml
	$(RM) .failed
	$(MAKE) -C src/machine-resources clean
	$(RM) -r $(VENV)
.PHONY: clean

clean+db: clean
	while fuser db --kill -TERM; do sleep 1; done
	$(RM) -r db
	$(RM) .db.lock
.PHONY: clean+db

harness: bin/maas-region bin/database
	$(dbrun) bin/maas-region shell --settings=maasserver.djangosettings.demo
.PHONY: harness

dbharness: bin/database
	bin/database --preserve shell
.PHONY: dbharness

syncdb: bin/maas-region bin/database
	$(dbrun) bin/maas-region dbupgrade
.PHONY: syncdb

#
# Development services.
#

service_names_region := database dns regiond reloader
service_names_rack := http rackd reloader
service_names_all := $(service_names_region) $(service_names_rack)

# The following template is intended to be used with `call`, and it
# accepts a single argument: a target name. The target name must
# correspond to a service action (see "Pseudo-magic targets" below). A
# region- and rack-specific variant of the target will be created, in
# addition to the target itself. These can be used to apply the service
# action to the region services, the rack services, or all services, at
# the same time.
define service_template
$(1)-region: $(patsubst %,services/%/@$(1),$(service_names_region))
$(1)-rack: $(patsubst %,services/%/@$(1),$(service_names_rack))
$(1): $(1)-region $(1)-rack
endef

# Expand out aggregate service targets using `service_template`.
$(eval $(call service_template,pause))
$(eval $(call service_template,restart))
$(eval $(call service_template,start))
$(eval $(call service_template,status))
$(eval $(call service_template,stop))
$(eval $(call service_template,supervise))

# The `run` targets do not fit into the mould of the others.
run-region:
	@services/run $(service_names_region)
.PHONY: run-region
run-rack:
	@services/run $(service_names_rack)
.PHONY: run-rack
run:
	@services/run $(service_names_all)
.PHONY: run

# Convenient variables and functions for service control.

setlock = $(call available,setlock,daemontools)
supervise = $(call available,supervise,daemontools)
svc = $(call available,svc,daemontools)
svok = $(call available,svok,daemontools)
svstat = $(call available,svstat,daemontools)

service_lock = $(setlock) -n /run/lock/maas.dev.$(firstword $(1))

# Pseudo-magic targets for controlling individual services.

services/%/@run: services/%/@stop services/%/@deps
	@$(call service_lock, $*) services/$*/run

services/%/@start: services/%/@supervise
	@$(svc) -u $(@D)

services/%/@pause: services/%/@supervise
	@$(svc) -d $(@D)

services/%/@status:
	@$(svstat) $(@D)

services/%/@restart: services/%/@supervise
	@$(svc) -du $(@D)

services/%/@stop:
	@if $(svok) $(@D); then $(svc) -dx $(@D); fi
	@while $(svok) $(@D); do sleep 0.1; done

services/%/@supervise: services/%/@deps
	@mkdir -p logs/$*
	@touch $(@D)/down
	@if ! $(svok) $(@D); then \
	    logdir=$(CURDIR)/logs/$* \
	        $(call service_lock, $*) $(supervise) $(@D) & fi
	@while ! $(svok) $(@D); do sleep 0.1; done

# Dependencies for individual services.

services/dns/@deps: bin/py bin/maas-common

services/database/@deps: bin/database

services/http/@deps: bin/py

services/rackd/@deps: bin/rackd bin/maas-rack bin/maas-common

services/reloader/@deps:

services/regiond/@deps: bin/maas-region bin/maas-rack bin/maas-common

#
# Package building
#

# This ought to be as simple as using
#   gbp buildpackage --git-debian-branch=packaging
# but it is not: without investing more time, we manually pre-build the source
# tree and run debuild.

packaging-repo = https://git.launchpad.net/maas/
packaging-branch = "packaging"

packaging-build-area := $(abspath ../build-area)
packaging-version := $(shell utilities/package-version)
tmp_changelog := $(shell tempfile)
packaging-dir := maas_$(packaging-version)
packaging-orig-tar := $(packaging-dir).orig.tar
packaging-orig-targz := $(packaging-dir).orig.tar.gz

machine_resources_vendor := src/machine-resources/src/machine-resources/vendor

-packaging-clean:
	rm -rf $(packaging-build-area)
	mkdir -p $(packaging-build-area)
.PHONY: -packaging-clean

-packaging-export-orig: $(packaging-build-area)
	git archive --format=tar $(packaging-export-extra) \
            --prefix=$(packaging-dir)/ \
	    -o $(packaging-build-area)/$(packaging-orig-tar) HEAD
	$(MAKE) machine-resources-vendor
	tar -rf $(packaging-build-area)/$(packaging-orig-tar) $(machine_resources_vendor) \
		--transform 's,^,$(packaging-dir)/,'
	gzip -f $(packaging-build-area)/$(packaging-orig-tar)
.PHONY: -packaging-export-orig

-packaging-export-orig-uncommitted: $(packaging-build-area)
	git ls-files --others --exclude-standard --cached | grep -v '^debian' | \
	    xargs tar --transform 's,^,$(packaging-dir)/,' -cf $(packaging-build-area)/$(packaging-orig-tar)
	$(MAKE) machine-resources-vendor
	tar -rf $(packaging-build-area)/$(packaging-orig-tar) $(machine_resources_vendor) \
		--transform 's,^,$(packaging-dir)/,'
	gzip -f $(packaging-build-area)/$(packaging-orig-tar)
.PHONY: -packaging-export-orig-uncommitted

-packaging-export: -packaging-export-orig$(if $(export-uncommitted),-uncommitted,)
.PHONY: -packaging-export

-package-tree: -packaging-export
	(cd $(packaging-build-area) && tar xfz $(packaging-orig-targz))
	(cp -r debian $(packaging-build-area)/$(packaging-dir))
	echo "maas ($(packaging-version)-0ubuntu1) UNRELEASED; urgency=medium" \
	    > $(tmp_changelog)
	tail -n +2 debian/changelog >> $(tmp_changelog)
	mv $(tmp_changelog) $(packaging-build-area)/$(packaging-dir)/debian/changelog
.PHONY: -package-tree

package-tree: assets -packaging-clean -package-tree

package: package-tree
	(cd $(packaging-build-area)/$(packaging-dir) && debuild -uc -us)
	@echo Binary packages built, see $(packaging-build-area).
.PHONY: package

# To build binary packages from uncommitted changes call "make package-dev".
package-dev:
	make export-uncommitted=yes package
.PHONY: package-dev

source-package: -package-tree
	(cd $(packaging-build-area)/$(packaging-dir) && debuild -S -uc -us)
	@echo Source package built, see $(packaging-build-area).
.PHONY: source-package

# To build source packages from uncommitted changes call "make package-dev".
source-package-dev:
	make export-uncommitted=yes source-package
.PHONY: source-package-dev

# To rebuild packages (i.e. from a clean slate):
package-rebuild: package-clean package
.PHONY: package-rebuild

package-dev-rebuild: package-clean package-dev
.PHONY: package--dev-rebuild

source-package-rebuild: source-package-clean source-package
.PHONY: source-package-rebuild

source-package-dev-rebuild: source-package-clean source-package-dev
.PHONY: source-package-dev-rebuild

# To clean built packages away:
package-clean: patterns := *.deb *.udeb *.dsc *.build *.changes
package-clean: patterns += *.debian.tar.xz *.orig.tar.gz
package-clean:
	@$(RM) -v $(addprefix $(packaging-build-area)/,$(patterns))
.PHONY: package-clean

source-package-clean: patterns := *.dsc *.build *.changes
source-package-clean: patterns += *.debian.tar.xz *.orig.tar.gz
source-package-clean:
	@$(RM) -v $(addprefix $(packaging-build-area)/,$(patterns))
.PHONY: source-package-clean

# Debugging target. Allows printing of any variable.
# As an example, try:
#     make print-scss_input
print-%:
	@echo $* = $($*)

#
# Snap building
#

snap-clean:
	$(snapcraft) clean
.PHONY: snap-clean

snap:
	$(snapcraft)
.PHONY: snap

#
# Helpers for using the snap for development testing.
#

build/dev-snap: ## Check out a clean version of the working tree.
	git checkout-index -a --prefix build/dev-snap/

build/dev-snap/prime: build/dev-snap
	cd build/dev-snap && $(snapcraft) prime --destructive-mode

sync-dev-snap: RSYNC=rsync -v -r -u -l -t -W -L
sync-dev-snap: build/dev-snap/prime
	$(RSYNC) --exclude 'maastesting' --exclude 'tests' --exclude 'testing' \
		--exclude 'machine-resources' --exclude '*.pyc' \
		--exclude '__pycache__' \
		src/ build/dev-snap/prime/lib/python3.6/site-packages/
	$(RSYNC) \
		src/maasserver/static/ build/dev-snap/prime/usr/share/maas/web/static/
	$(RSYNC) snap/local/tree/ build/dev-snap/prime
.PHONY: sync-dev-snap
