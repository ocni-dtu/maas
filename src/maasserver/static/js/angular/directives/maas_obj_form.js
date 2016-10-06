/* Copyright 2016 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * MAAS object directive.
 *
 * Directive that connects a field to an object from the websocket. The field
 * is an instant save on blur.
 */

angular.module('MAAS').directive('maasObjForm', ['JSONService',
    function(JSONService) {
        function MAASFormController(scope) {
            this.obj = scope.obj;
            this.manager = scope.manager;
            this.fields = {};
            this.scope = scope;
            this.scope.saving = false;
            if(angular.isObject(this.scope.obj)) {
                this.scope.obj.$maasForm = this;
            }

            // Set the managerMethod.
            this.managerMethod = scope.managerMethod;
            if(angular.isUndefined(this.managerMethod)) {
                this.managerMethod = "updateItem";
            }

            var self = this;
            scope.$watch("obj", function() {
                // Update the object when it changes.
                self.obj = scope.obj;
                if(angular.isObject(self.obj)) {
                    self.obj.$maasForm = self;
                }
            });
            scope.$on("$destroy", function() {
                // Remove the $maasForm from the object when directive is
                // deleted.
                if(angular.isObject(self.obj)) {
                    delete self.obj.$maasForm;
                }
            });
        }

        // Get the current value for a field in the form.
        MAASFormController.prototype.getValue = function (key) {
            var field = this.fields[key];
            if(angular.isObject(field) && angular.isObject(field.scope)) {
                return field.scope.getValue();
            }
        };

        // Clone the current object for this form without the $maasForm
        // property set.
        MAASFormController.prototype.cloneObject = function() {
            if(!angular.isObject(this.obj)) {
                return this.obj;
            } else {
                delete this.obj.$maasForm;
                var clonedObj = angular.copy(this.obj);
                this.obj.$maasForm = this;
                return clonedObj;
            }
        };

        // Return true if table form.
        MAASFormController.prototype.isTableForm = function () {
            if(angular.isUndefined(this.scope.tableForm)) {
                // Default is not a table form.
                return false;
            } else {
                return this.scope.tableForm;
            }
        };

        // Return true if the form should be saved on blur.
        MAASFormController.prototype.saveOnBlur = function() {
            if(angular.isUndefined(this.scope.saveOnBlur)) {
                // Default is save on blur.
                return true;
            } else {
                return this.scope.saveOnBlur;
            }
        };

        // Return true if any field in the form as an error.
        MAASFormController.prototype.hasErrors = function() {
            var hasErrors = false;
            angular.forEach(this.fields, function(field) {
                if(field.scope.hasErrors()) {
                    hasErrors = true;
                }
            });
            if(angular.isDefined(this.errorScope)) {
                if(this.errorScope.hasErrors()) {
                    hasErrors = true;
                }
            }
            return hasErrors;
        };

        // Called by maas-obj-field to register it as a editable field.
        MAASFormController.prototype.registerField = function(key, scope) {
            // Store the state of the field and its scope.
            this.fields[key] = {
                editing: false,
                scope: scope
            };

            // Watch for changes on the value of the object.
            var self = this;
            this.scope.$watch("obj." + key, function() {
                if(angular.isObject(self.obj) && !self.fields[key].editing) {
                    self.fields[key].scope.updateValue(self.obj[key]);
                }
            });

            // Return the current value for the field.
            if(angular.isObject(this.obj)) {
                return this.obj[key];
            } else {
                return null;
            }
        };

        // Called by maas-obj-field to place field in edit mode.
        MAASFormController.prototype.startEditingField = function(key) {
            this.fields[key].editing = true;
        };

        // Called by maas-obj-field to end edit mode for the field.
        MAASFormController.prototype.stopEditingField = function(key, value) {
            var field = this.fields[key];

            // Do nothing if not save on blur.
            if(!this.saveOnBlur()) {
                field.editing = false;
                return;
            }

            // Clear errors before saving.
            field.scope.clearErrors();

            // Copy the object and update the editing field.
            var updatedObj = this.cloneObject();
            updatedObj[key] = value;
            if(updatedObj[key] === this.obj[key]) {
                // Nothing changed.
                field.editing = false;
                return;
            }

            // Update the item in the manager.
            this.scope.saving = true;
            this.updateItem(updatedObj, [key]);
        };

        // Update the item using the manager.
        MAASFormController.prototype.updateItem = function(updatedObj, keys) {
            var key = keys[0];
            var field = this.fields[key];
            var self = this;

            // Pre-process the updatedObj if one is defined.
            if(angular.isFunction(this.scope.preProcess)) {
                updatedObj = this.scope.preProcess(updatedObj, keys);
            }

            // Update the item with the manager.
            return this.manager[this.managerMethod](
                updatedObj).then(function(newObj) {
                    // Update the value of the element.
                    field.editing = false;
                    field.scope.updateValue(newObj[key]);
                    self.scope.saving = false;
                    if(angular.isFunction(self.scope.afterSave)) {
                        self.scope.afterSave(newObj);
                    }
                    return newObj;
                }, function(error) {
                    var errorJson = JSONService.tryParse(error);
                    if(angular.isObject(errorJson)) {
                        // Add the error to each field it matches.
                        angular.forEach(errorJson, function(value, key) {
                            var errorField = self.fields[key];
                            if(!angular.isArray(value)) {
                                value = [value];
                            }

                            if(angular.isObject(errorField)) {
                                // Error on a field we know about, place the
                                // error on that field.
                                errorField.scope.setErrors(value);
                            } else {
                                // Error on a field we don't know about, place
                                // the error on the editing field. Prefixing
                                // the error with the field.
                                if(key !== "__all__") {
                                    value = value.map(function(v) {
                                        return key + ": " + v;
                                    });
                                }
                                field.scope.setErrors(value);
                            }
                        });
                    } else {
                        // Add the string error to just the field error.
                        field.scope.setErrors([error]);
                    }
                    self.scope.saving = false;
                    return error;
                });
        };

        // Called when saveOnBlur is false to save the whole form.
        MAASFormController.prototype.saveForm = function () {
            var keys = [];
            var updatedObj = this.cloneObject();
            angular.forEach(this.fields, function(value, key) {
                value.scope.clearErrors();
                var newValue = value.scope.getValue();
                if(angular.isDefined(newValue) &&
                    updatedObj[key] !== newValue) {
                    updatedObj[key] = newValue;
                    keys.push(key);
                }
            });

            // Pre-process the updatedObj if one is defined.
            if(angular.isFunction(this.scope.preProcess)) {
                updatedObj = this.scope.preProcess(updatedObj, keys);
            }

            // Clear the errors on the errorScope before save.
            if(angular.isDefined(this.errorScope)) {
                this.errorScope.clearErrors();
            }

            var self = this;
            this.scope.saving = true;
            return this.manager[this.managerMethod](
                updatedObj).then(function(newObj) {
                    self.scope.saving = false;
                    if(angular.isFunction(self.scope.afterSave)) {
                        self.scope.afterSave(newObj);
                    }
                    return newObj;
                }, function(error) {
                    var errorJson = JSONService.tryParse(error);
                    if(angular.isObject(errorJson)) {
                        // Add the error to each field it matches.
                        angular.forEach(errorJson, function(value, key) {
                            var errorField = self.fields[key];
                            if(!angular.isArray(value)) {
                                value = [value];
                            }

                            if(angular.isObject(errorField)) {
                                // Error on a field we know about, place the
                                // error on that field.
                                errorField.scope.setErrors(value);
                            } else {
                                if(key !== "__all__") {
                                    value = value.map(function(v) {
                                        return key + ": " + v;
                                    });
                                }
                                // Error on a field we don't know about, place
                                // the error on errorScope if set.
                                if(angular.isDefined(self.errorScope)) {
                                    self.errorScope.setErrors(value);
                                } else {
                                    // No error scope, just log to console.
                                    console.log(value);
                                }
                            }
                        });
                    } else {
                        // Add the string error to just the field error.
                        if(angular.isDefined(self.errorScope)) {
                            self.errorScope.setErrors([error]);
                        } else {
                            // No error scope, just log to console.
                            console.log(error);
                        }
                    }
                    self.scope.saving = false;
                    return error;
                });
        };

        return {
            restrict: "E",
            scope: {
                obj: "=",
                manager: "=",
                managerMethod: "@",
                preProcess: "=",
                afterSave: "=",
                tableForm: "=",
                saveOnBlur: "=",
                inline: "=",
                ngDisabled: "&"
            },
            transclude: true,
            template: (
                '<form class="form" ng-class="{saving: saving, ' +
                '\'form--inline\': inline, ' +
                '\'form--stack\': tableForm}" ' +
                'ng-transclude></form>'),
            controller: ['$scope', MAASFormController]
        };
    }]);

angular.module('MAAS').directive('maasObjFieldGroup', ['JSONService',
    function(JSONService) {
        // Controller for this directive.
        function MAASGroupController(scope, timeout) {
            this.fields = {};
            this.scope = scope;
            this.timeout = timeout;

            var self = this;
            this.scope.isEditing = function() {
                var editing = false;
                angular.forEach(self.fields, function(value) {
                    if(!editing) {
                        editing = value.editing;
                    }
                });
                return editing;
            };
        }

        // Return true if table form.
        MAASGroupController.prototype.isTableForm = function () {
            return this.formController.isTableForm();
        };

        // Return true if should save on blur.
        MAASGroupController.prototype.saveOnBlur = function () {
            return this.formController.saveOnBlur();
        };

        // Called by maas-obj-field to register it as a editable field.
        MAASGroupController.prototype.registerField = function(key, scope) {
            // Store the state of the field and its scope.
            this.fields[key] = {
                editing: false,
                scope: scope
            };
            return this.formController.registerField(key, scope);
        };

        // Called by maas-obj-field to place field in edit mode.
        MAASGroupController.prototype.startEditingField = function(key) {
            this.fields[key].editing = true;

            // Set all fields in the group as editing in the formController.
            var self = this;
            angular.forEach(this.fields, function(value, key) {
                self.formController.startEditingField(key);
            });
        };

        // Called by maas-obj-field to exit edit mode for the field.
        MAASGroupController.prototype.stopEditingField = function(key, value) {
            var field = this.fields[key];
            field.editing = false;

            // Exit early if not save on blur.
            if(!this.saveOnBlur()) {
                return;
            }

            // Delay the handling of stop to make sure start is not called on
            // the next field in the group.
            var self = this;
            this.timeout(function() {
                // If any other fields are in edit mode then nothing to do.
                var editing = false;
                angular.forEach(self.fields, function(value) {
                    if(!editing) {
                        editing = value.editing;
                    }
                });
                if(editing) {
                    return;
                }

                // Copy the object and update the editing fields.
                var keys = [];
                var changed = false;
                var updatedObj = self.formController.cloneObject();
                angular.forEach(self.fields, function(value, key) {
                    value.scope.clearErrors();
                    var newValue = value.scope.getValue();
                    if(angular.isDefined(newValue) &&
                        updatedObj[key] !== newValue) {
                        keys.push(key);
                        updatedObj[key] = newValue;
                        changed = true;
                    }
                });
                if(!changed) {
                    return;
                }

                // Place the field that actually triggered the update first.
                var keyIdx = keys.indexOf(key);
                if(keyIdx !== -1) {
                    keys.splice(keyIdx, 1);
                    keys.splice(0, 0, key);
                }

                // Save the object.
                self.formController.updateItem(updatedObj, keys);
            }, 10); // Really short has to be next click.
        };

        return {
            restrict: "E",
            require: ["^^maasObjForm", "maasObjFieldGroup"],
            scope: {},
            transclude: true,
            template: (
                '<div class="form__siblings" ' +
                'ng-class="{\'is-active\': isEditing()}" ' +
                'ng-transclude></div>'),
            controller: ['$scope', '$timeout', MAASGroupController],
            link: {
                pre: function(scope, element, attrs, controllers) {
                    // Set formController on the MAASGroupController to
                    // point to its parent MAASFormController. This is done in
                    // pre-link so the controller has the formController before
                    // registerField is called.
                    controllers[1].formController = controllers[0];

                    // Set ngDisabled on this scope from the form controller.
                    scope.ngDisabled = controllers[0].scope.ngDisabled;
                }
            }
        };
    }]);

angular.module('MAAS').directive('maasObjField', ['$compile',
    function($compile) {
        return {
            restrict: "E",
            require: ["^^maasObjForm", "?^^maasObjFieldGroup"],
            scope: {},
            transclude: true,
            template: (
                '<div ng-transclude></div>'),
            link: function(scope, element, attrs, controllers) {
                // Select the controller based on which is available.
                var controller = controllers[1];
                if(!angular.isObject(controller)) {
                    controller = controllers[0];
                }

                // Set ngDisabled from the parent controller.
                scope.ngDisabled = controller.scope.ngDisabled;

                element.addClass("form__group");
                if(attrs.subtle !== "false") {
                    element.addClass("form__group--subtle");
                }

                // type and key required.
                var missingAttrs = [];
                if(!angular.isString(attrs.type) && attrs.type.length === 0) {
                    missingAttrs.push("type");
                }
                if(!angular.isString(attrs.key) && attrs.key.length === 0) {
                    missingAttrs.push("key");
                }
                if(missingAttrs.length > 0) {
                    throw new Error(
                        missingAttrs.join(", ") +
                        " are required on maas-obj-field.");
                }

                // Remove transcluded element.
                element.find("div").remove();

                // Render the label.
                var label = attrs.label || attrs.key;

                if(attrs.disableLabel !== "true") {
                    var labelElement = angular.element('<label/>');
                    labelElement.attr('for', attrs.key);
                    labelElement.text(label);
                    labelElement.addClass('form__group-label');
                    if(attrs.labelWidth) {
                        labelElement.addClass(attrs.labelWidth + "-col");
                    } else {
                        labelElement.addClass("u-margin--right");
                    }
                    element.append(labelElement);

                    // Add a label info icon with tooltip.
                    if(angular.isString(attrs.labelInfo)
                        && attrs.labelInfo.length > 0) {
                        var infoElement = angular.element('<i/>');
                        infoElement.addClass('icon');
                        infoElement.addClass('icon--info');
                        infoElement.addClass('tooltip');
                        infoElement.attr('aria-label', attrs.labelInfo);
                        labelElement.text(labelElement.text() + ' ');
                        labelElement.append(infoElement);
                    }
                }

                // Add the wrapper for the input.
                var inputWrapper = angular.element('<div></div>');
                inputWrapper.addClass("form__group-input");

                if(attrs.inputWidth) {
                    inputWrapper.addClass(attrs.inputWidth + "-col");
                    inputWrapper.addClass("last-col");
                }

                // Render the input based on the type.
                var placeholder = attrs.placeholder || label;
                var inputElement = null;
                if(attrs.type === "text" || attrs.type === "textarea") {
                    if(attrs.type === "text") {
                        inputElement = $compile(
                            '<input type="text" id="' + attrs.key +
                            '" placeholder="' + placeholder + '"' +
                            'ng-disabled="ngDisabled()">')(scope);
                    } else if(attrs.type === "textarea") {
                        inputElement = $compile(
                            '<textarea id="' + attrs.key +
                            '" placeholder="' + placeholder + '"' +
                            'ng-disabled="ngDisabled()"></textarea>')(scope);
                    }

                    // Allow enter on blur, by default.
                    if(attrs.blurOnEnter) {
                        inputElement.bind("keydown keypress", function(evt) {
                            if(evt.which === 13) {
                                inputElement.blur();
                                evt.preventDefault();
                            }
                        });
                    }

                    // Revert value on esc.
                    var self = this;
                    inputElement.bind("keydown keypress", function(evt) {
                        if(evt.which === 27) {
                            inputElement.val(controller.scope.obj[attrs.key]);
                            inputElement.blur();
                            evt.preventDefault();
                        }
                    });

                    // Register the field with the controller and set the
                    // current value for the field.
                    var currentValue = controller.registerField(
                        attrs.key, scope);
                    inputElement.val(currentValue);

                    // When element is in focus then editing is on.
                    inputElement.on("focus", function() {
                        scope.$apply(function() {
                            controller.startEditingField(attrs.key);
                        });
                    });

                    // When element is not in focus then editing is done.
                    inputElement.on("blur", function() {
                        scope.$apply(function() {
                            controller.stopEditingField(
                                attrs.key, inputElement.val());
                        });
                    });

                    // Called by controller to update the value.
                    scope.updateValue = function(newValue) {
                        inputElement.val(newValue);
                    };

                    // Called by controller to get the value.
                    scope.getValue = function() {
                        return inputElement.val();
                    };
                } else if(attrs.type === "options") {
                    // Requires the options attribute on the element. This
                    // is copied directly into the ngOptions directive on
                    // the select.
                    var options = attrs.options;
                    if(!angular.isString(options) || options.length === 0) {
                        throw new Error(
                            "options attribute is required on type " +
                            "'options' on maas-obj-field.");
                    }

                    // Placeholder by default is disabled, allow it to be
                    // enabled.
                    var disabled = "disabled";
                    if(attrs.placeholderEnabled === "true") {
                        disabled = "";
                    }

                    // Create a child scope of the parent scope for this
                    // directive. Since this directive is created with an
                    // isolated scope we need the child to use the parent so
                    // ngOptions can use properties defined in that scope.
                    var childScope = scope.$parent.$new();
                    childScope._ngDisabled = scope.ngDisabled;
                    childScope._selectValue = controller.registerField(
                        attrs.key, scope);
                    childScope._selectNgChange = function() {
                        controller.stopEditingField(
                            attrs.key, childScope._selectValue);
                    };

                    // Construct the select.
                    inputElement = $compile(
                        '<select id="' + attrs.key + '" ' +
                        'ng-model="_selectValue" ' +
                        'ng-options="' + options + '"' +
                        'ng-change="_selectNgChange()"' +
                        'ng-disabled="_ngDisabled()">' +
                        '<option value="" ' + disabled + '>' + placeholder +
                        '</option></select>')(childScope);

                    // Called by controller to update the value.
                    scope.updateValue = function(newValue) {
                        childScope._selectValue = newValue;
                    };

                    // Called by controller to get the value.
                    scope.getValue = function() {
                        return childScope._selectValue;
                    };
                } else if(attrs.type === "checkboxes") {
                    // Requires the values attribute on the element.
                    var values = attrs.values;
                    if(!angular.isString(values) || values.length === 0) {
                        throw new Error(
                            "values attribute is required on type " +
                            "'checkboxes' on maas-obj-field.");
                    }

                    // Create a child scope of the parent scope for this
                    // directive. Since this directive is created with an
                    // isolated scope we need the child to use the parent so
                    // values can come from the parent scope.
                    var checkScope = scope.$parent.$new();
                    checkScope._selectedValues = controller.registerField(
                        attrs.key, scope);
                    checkScope._checked = function(val) {
                        return checkScope._selectedValues.indexOf(val) > -1;
                    };
                    checkScope._toggleChecked = function(val) {
                        var idx = checkScope._selectedValues.indexOf(val);
                        if(idx > -1) {
                            // Uncheck.
                            checkScope._selectedValues.splice(idx, 1);
                        } else {
                            // Check.
                            checkScope._selectedValues.push(val);
                        }
                    };

                    // Construct the checkbox list.
                    inputElement = angular.element([
                        '<div class="width--full" ',
                            'ng-repeat="val in ' + values +'">',
                            '<input id="' + attrs.key + '_' + '{$ val $}',
                                '" type="checkbox" value="{$ val $}" ',
                                'class="checkbox" ',
                                'ng-checked="_checked(val)" ',
                                'ng-click="_toggleChecked(val)">',
                            '<label for="' + attrs.key + '_',
                                '{$ val $}' + '" ',
                                'class="checkbox-label">{$ val $}</label>',
                        '</div>'
                    ].join(''));
                    inputElement = $compile(inputElement)(checkScope);

                    // Called by controller to update the value.
                    scope.updateValue = function(newValue) {
                        checkScope._selectedValues = newValue;
                    };

                    // Called by controller to get the value.
                    scope.getValue = function() {
                        return checkScope._selectedValues;
                    };
                } else if(attrs.type === "tags") {
                    var tagsScope = scope.$new();
                    var tags = controller.registerField(
                        attrs.key, scope);
                    tagsScope._tags = tags.map(function(val) {
                        return {text: val};
                    });

                    // Construct the tags input.
                    inputElement = angular.element([
                        '<tags-input id="' + attrs.key + '" ',
                            'ng-model="_tags" ',
                            'placeholder="' + placeholder + '" ',
                            'allow-tags-pattern="[\\w-]+"></tags-input>'
                    ].join(''));
                    inputElement = $compile(inputElement)(tagsScope);

                    // Called by controller to update the value.
                    scope.updateValue = function(newValue) {
                        tagsScope._tags = newValue.map(
                            function(val) {
                                return {text: val};
                            });
                    };

                    // Called by controller to get the value.
                    scope.getValue = function() {
                        return tagsScope._tags.map(
                            function(val) {
                                return val.text;
                            });
                    };
                } else if(attrs.type === "onoffswitch") {
                    var switchScope = scope.$new();
                    switchScope._toggle = controller.registerField(
                        attrs.key, scope);
                    switchScope._changed = function() {
                        controller.startEditingField(attrs.key);
                        controller.stopEditingField(
                            attrs.key, switchScope.getValue());
                    };

                    // Construct the on and off toggle.
                    inputElement = angular.element([
                        '<div class="onoffswitch">',
                            '<input type="checkbox" name="' + attrs.key + '" ',
                                'class="onoffswitch-checkbox" ',
                                'id="' + attrs.key + '" ',
                                'ng-model="_toggle" ',
                                'ng-change="_changed()">',
                            '<label class="onoffswitch-label" ',
                                'for="' + attrs.key + '">',
                                '<span class="onoffswitch-inner"></span>',
                                '<span class="onoffswitch-switch"></span>',
                            '</label>',
                        '</div>'
                    ].join(''));
                    inputElement = $compile(inputElement)(switchScope);

                    // Called by controller to update the value.
                    scope.updateValue = function(newValue) {
                        // WARNING: This code is difficult to unit test, since
                        // we could not figure out how to get the
                        // isolateScope() from the transcluded element. Be sure
                        // to manually test versions of this toggle with and
                        // without the on-value and off-value attributes, such
                        // as by verifying that both the on/off toggle on both
                        // the discovery page and the subnet details page work.
                        if(attrs.onValue && attrs.onValue === newValue) {
                            switchScope._toggle = true;
                        } else if(attrs.offValue &&
                                attrs.offValue === newValue) {
                            switchScope._toggle = false;
                        } else {
                            switchScope._toggle = newValue;
                        }
                    };

                    // Called by controller to get the value.
                    scope.getValue = function() {
                        // WARNING: This code is difficult to unit test, since
                        // we could not figure out how to get the
                        // isolateScope() from the transcluded element. Be sure
                        // to manually test versions of this toggle with and
                        // without the on-value and off-value attributes, such
                        // as by verifying that both the on/off toggle on both
                        // the discovery page and the subnet details page work.
                        if(switchScope._toggle) {
                            if(attrs.onValue) {
                                return attrs.onValue;
                            } else {
                                return true;
                            }
                        } else {
                            if(attrs.offValue) {
                                return attrs.offValue;
                            } else {
                                return false;
                            }
                        }
                    };
                } else {
                    throw new Error(
                        "Unknown type on maas-obj-field: " + attrs.type);
                }

                // Copy input class to the input element.
                if(attrs.inputClass) {
                    inputElement.addClass(attrs.inputClass);
                }
                inputWrapper.append(inputElement);

                // Errors element.
                var errorsElement = angular.element(
                    '<ul class="form__error"></ul>');
                if(!controller.isTableForm()) {
                    errorsElement.addClass("form__error");
                }
                inputWrapper.append(errorsElement);
                element.append(inputWrapper);

                // Called by controller to clear all errors.
                scope.clearErrors = function() {
                    inputElement.removeClass("has-error");
                    inputElement.removeClass("ng-dirty");
                    errorsElement.empty();
                };

                // Called by controller to set errors.
                scope.setErrors = function(errors) {
                    if(errors.length > 0) {
                        inputElement.addClass("has-error");
                        inputElement.addClass("ng-dirty");
                        angular.forEach(errors, function(error) {
                            errorsElement.append(
                              '<li class="form__error-item">' + error + '</li>'
                            );
                        });
                        // Set the input in focus but outside of the current
                        // digest cycle.
                        setTimeout(function() {
                            inputElement.focus();
                        }, 1);
                    }
                };

                // Called by controller to see if error is set on field.
                scope.hasErrors = function() {
                    return inputElement.hasClass("has-error");
                };
            }
        };
    }]);

angular.module('MAAS').directive('maasObjSave', function() {
        return {
            restrict: "A",
            require: ["^^maasObjForm"],
            scope: {},
            link: function(scope, element, attrs, controllers) {
                // Only allow maas-obj-save when saveOnBlur is false.
                var controller = controllers[0];
                if(controller.saveOnBlur()) {
                    throw new Error(
                        "maas-obj-save is only allowed when save-on-blur is " +
                        "set to false.");
                }

                element.on("click", function() {
                    controller.saveForm();
                });
            }
        };
    });

angular.module('MAAS').directive('maasObjErrors', function() {
        return {
            restrict: "E",
            require: ["^^maasObjForm"],
            scope: {},
            template: '<ul class="form__error"></ul>',
            link: function(scope, element, attrs, controllers) {
                // Set on the controller the global error handler.
                controllers[0].errorScope = scope;
                var ul = element.find("ul");

                // Called by controller to clear all errors.
                scope.clearErrors = function() {
                    ul.empty();
                };

                // Called by controller to set errors.
                scope.setErrors = function(errors) {
                    if(errors.length > 0) {
                        angular.forEach(errors, function(error) {
                            ul.append(
                              '<li class="form__error-item">' + error + '</li>'
                            );
                        });
                    }
                };

                // Called by controller to see if error is set on field.
                scope.hasErrors = function() {
                    return ul.children().length > 0;
                };
            }
        };
    });