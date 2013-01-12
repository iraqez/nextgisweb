define([
    "dojo/_base/declare",
    "dijit/_WidgetBase",
    "dijit/_TemplatedMixin",
    "dijit/_WidgetsInTemplateMixin",
    "dojo/text!./templates/Display.html",
    "dojo/_base/array",
    "dojo/Deferred",
    "dojox/geo/openlayers/Map",
    "dijit/form/DropDownButton",
    "dijit/DropDownMenu",
    "dijit/MenuItem",
    "dijit/layout/ContentPane",
    // дерево слоев
    "dojo/data/ItemFileWriteStore",
    "cbtree/models/TreeStoreModel",
    "cbtree/Tree",
    "dijit/tree/dndSource",
    // template
    "dijit/layout/TabContainer",
    "dijit/layout/BorderContainer",
    "dijit/layout/ContentPane",
    "dijit/Toolbar",
    "dijit/form/Button",
    "dijit/form/Select",
    "dijit/form/DropDownButton"
], function (
    declare,
    _WidgetBase,
    _TemplatedMixin,
    _WidgetsInTemplateMixin,
    template,
    array,
    Deferred,
    Map,
    DropDownButton,
    DropDownMenu,
    MenuItem,
    ContentPane,
    ItemFileWriteStore,
    TreeStoreModel,
    Tree,
    dndSource
) {
    var CustomItemFileWriteStore = declare([ItemFileWriteStore], {
        dumpItem: function (item) {
            var obj = {};

            if (item) {
                var attributes = this.getAttributes(item);

                if (attributes && attributes.length > 0) {
                    var i;

                    for(i = 0; i < attributes.length; i++){
                        var values = this.getValues(item, attributes[i]);

                        if (values) {
                            if(values.length > 1 ){
                                var j;

                                obj[attributes[i]] = [];
                                for (j = 0; j < values.length; j++ ) {
                                    var value = values[j];

                                    if (this.isItem(value)) {
                                        obj[attributes[i]].push(this.dumpItem(value));
                                    } else {
                                        obj[attributes[i]].push(value);
                                    };
                                };
                            } else {
                                if (this.isItem(values[0])) {
                                    obj[attributes[i]] = this.dumpItem(values[0]);
                                } else {
                                    obj[attributes[i]] = values[0];
                                };
                            };
                        };
                    };
                };
            };

            return obj;
        }
    });

    return declare([_WidgetBase, _TemplatedMixin, _WidgetsInTemplateMixin], {
        templateString: template,

        constructor: function (options) {
            this.treeConfig = options.treeConfig;
            this.layerConfig = options.layerConfig;

            // Хранилище значений для дерева слоев
            this._treeStore = new CustomItemFileWriteStore({
                data: { 
                    label: "display_name",
                    items: [ options.treeConfig ]
                }
            }); 

            // Модель данных для дерева слоев
            this._treeModel = new TreeStoreModel({
                store: this._treeStore,
                query: {item_type: 'root'},
                checkedAll: false
            });

            // Из за необходимости модели дерево не получается создать
            // в декларативном стиле, создаем вручную
            this.treeWidget = new Tree({
                model: this._treeModel,
                autoExpand: true,
                showRoot: false,
                branchReadOnly: true,
                dndController: dndSource
            });

            this._layers = {};

            var display = this;

            this.treeWidget.on("checkBoxClick", function (item, nodeWidget, evt) {
                display._layers[item.id].olLayer.setVisibility(nodeWidget.get("checked"));
            });

            array.forEach(this.layerConfig, function (l) {
                display._layers[l.id] = new options.adapterClasses.tms(l);
            });

            this.loadLayerPlugins();

        },

        startup: function () {
            this.inherited(arguments);

            // Размещаем дерево на теле виджета
            this.treeWidget.placeAt(this.layerTreePane);

            // Инициализируем карту, без DOM она похоже не умеет
            this.map = new Map(this.mapNode);

            // Добавляем OL-слои на веб-карту
            var display = this;
            array.forEach(layerConfig, function(l) {
                display.map.olMap.addLayer(display._layers[l.id].olLayer);
            });

            // Плагины
            this.loadLayerPlugins();            
        },

        loadLayerPlugins: function () {
            this.layerPlugins = {}

            var display = this;
            array.forEach(this.layerConfig, function (lcfg) {
                array.forEach(Object.keys(lcfg.plugins), function (plugin_mid) {
                    if ( display.layerPlugins[plugin_mid] == undefined ) {
                        display.layerPlugins[plugin_mid] = null;
                        require([plugin_mid], function (plugin_mod) {
                            if ( display.layerPlugins[plugin_mid] == undefined ) {
                                var plugin = new plugin_mod({
                                    webmapDisplay: display,
                                    identity: plugin_mid
                                });
                                display.layerPlugins[plugin_mid] = plugin;
                                plugin.postCreate();
                            };
                        });
                    }
                });
            });
        }
    });
});