# -*- encoding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2010 Smile (<http://www.smile.fr>). All Rights Reserved
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import time

from openerp import api, fields, models, SUPERUSER_ID, tools, _
from openerp.exceptions import Warning
from openerp.modules.registry import Registry
from openerp.tools.safe_eval import safe_eval as eval

from checklist_decorators import checklist_view_decorator, checklist_create_decorator, checklist_write_decorator


def update_checklists(load):
    def wrapper(self, cr, module):
        res = load(self, cr, module)
        if self.get('checklist'):
            cr.execute("select relname from pg_class where relname='checklist'")
            if cr.rowcount:
                self.get('checklist')._update_models(cr, SUPERUSER_ID)
        return res
    return wrapper


class Checklist(models.Model):
    _name = 'checklist'
    _description = 'Checklist'

    name = fields.Char(size=128, required=True, translate=True)
    model_id = fields.Many2one('ir.model', 'Model', required=True)
    model = fields.Char(related='model_id.model', readonly=True)
    active = fields.Boolean('Active', default=True)
    active_field = fields.Boolean("Has an 'Active' field", compute='_get_active_field', default=False)
    action_id = fields.Many2one('ir.actions.server', 'Actions')
    act_window_ids = fields.Many2many('ir.actions.act_window', 'checklist_act_window_rel', 'act_window_id', 'checklist_id', 'Menus')
    view_ids = fields.Many2many('ir.ui.view', 'checklist_view_rel', 'view_id', 'checklist_id', 'Views')
    task_ids = fields.One2many('checklist.task', 'checklist_id', 'Tasks')

    @api.one
    def _get_active_field(self):
        if self.model_id:
            model = self.env[self.model_id.model]
            self.active_field = 'active' in model._fields.keys() + model._columns.keys()

    @api.one
    @api.constrains('model_id')
    def _check_unique_checklist_per_object(self):
        count = self.search_count([('model_id', '=', self.model_id.id)])
        if count > 1:
            raise Warning(_('A checklist has already existed for this model !'))

    @tools.cache(skiparg=3)
    def _get_checklist_by_model(self, cr, uid):
        res = {}
        ids = self.search(cr, SUPERUSER_ID, [], context={'active_test': True})
        for checklist in self.browse(cr, SUPERUSER_ID, ids):
            res[checklist.model] = checklist.id
        return res

    @staticmethod
    def _get_checklist_task_inst(self):
        domain = [('task_id.checklist_id.model_id.model', '=', self._name), ('res_id', '=', self.id)]
        self.checklist_task_instance_ids = self.env['checklist.task.instance'].with_context(active_test=True).search(domain)

    @api.model
    def _update_models(self, models=None):
        if not models:
            models = dict([(checklist.model_id, checklist) for checklist in self.with_context(active_test=True).search([])])
        for model, checklist in models.iteritems():
            if model.model not in self.env.registry.models:
                continue
            model_obj = self.env[model.model]
            if checklist:
                cls = model_obj.__class__
                setattr(cls, '_get_checklist_task_inst', api.one(api.depends()(Checklist._get_checklist_task_inst)))
                model_obj._add_field('checklist_task_instance_ids', fields.One2many('checklist.task.instance',
                                                                                    string='Checklist Task Instances',
                                                                                    compute='_get_checklist_task_inst'))
                self.pool.setup_models(self._cr, partial=(not self.pool.ready))
                model_obj._add_field('total_progress_rate', fields.Float('Progress Rate', digits=(16, 2)))
                model_obj._add_field('total_progress_rate_mandatory', fields.Float('Mandatory Progress Rate', digits=(16, 2)))
                model_pool = self.pool[model.model]
                model_pool._field_create(self._cr, self._context)
                model_pool._auto_init(self._cr, self._context)
            else:
                for field in ('checklist_task_instance_ids', 'total_progress_rate', 'total_progress_rate_mandatory'):
                    if field in model_obj._columns:
                        del model_obj._columns[field]
                    if field in model_obj._fields:
                        del model_obj._fields[field]
            if model_obj.create.__name__ != 'checklist_wrapper':
                model_obj._patch_method('create', checklist_create_decorator())
            if model_obj.write.__name__ != 'checklist_wrapper':
                model_obj._patch_method('write', checklist_write_decorator())
            if model_obj.fields_view_get.__name__ != 'checklist_wrapper':
                model_obj._patch_method('fields_view_get', checklist_view_decorator())
        self.clear_caches()

    def __init__(self, pool, cr):
        super(Checklist, self).__init__(pool, cr)
        setattr(Registry, 'load', update_checklists(getattr(Registry, 'load')))

    @api.model
    def create(self, vals):
        checklist = super(Checklist, self).create(vals)
        self._update_models({self.env['ir.model'].browse(vals['model_id']): checklist})
        return checklist

    @api.multi
    def write(self, vals):
        if 'model_id' in vals or 'active' in vals:
            models = dict([(checklist.model_id, False) for checklist in self])
            if vals.get('model_id'):
                models.update({self.env['ir.model'].browse(vals['model_id']): checklist})
        result = super(Checklist, self).write(vals)
        if 'model_id' in vals or 'active' in vals:
            self._update_models(models)
        return result

    @api.multi
    def unlink(self):
        models = dict([(checklist.model_id, False) for checklist in self])
        result = super(Checklist, self).unlink()
        self._update_models(models)
        return result

    @api.one
    def compute_progress_rates(self, records=None):
        if self._context.get('do_no_compute_progress_rates'):
            return
        if not records:
            records = self.env[self.model].with_context(active_test=False).search([])
        for record in records.with_context(active_test=True, no_checklist=True):
            ctx = {'active_id': record.id, 'active_ids': [record.id]}
            for task_inst in record.checklist_task_instance_ids:
                old_progress_rate = task_inst.progress_rate
                if task_inst.task_id.field_ids:
                    task_inst.progress_rate = 100.0 * len(task_inst.field_ids_filled) / len(task_inst.task_id.field_ids)
                else:
                    task_inst.progress_rate = 100.0
                if task_inst.task_id.action_id and old_progress_rate != task_inst.progress_rate == 100.0:
                    task_inst.task_id.action_id.with_context(**ctx).run()
            total_progress_rate = 0.0
            if record.checklist_task_instance_ids:
                total_progress_rate = sum(i.progress_rate for i in record.checklist_task_instance_ids) \
                    / len(record.checklist_task_instance_ids)
            vals = {'total_progress_rate': total_progress_rate}
            if self.active_field:
                total_progress_rate_mandatory = 100.0
                mandatory_inst = [i for i in record.checklist_task_instance_ids if i.mandatory]
                if mandatory_inst:
                    total_progress_rate_mandatory = sum(i.progress_rate for i in record.checklist_task_instance_ids if i.mandatory) \
                        / len(mandatory_inst)
                vals['total_progress_rate_mandatory'] = total_progress_rate_mandatory
                vals['active'] = total_progress_rate_mandatory == 100.0
            old_total_progress_rate = record.total_progress_rate
            record.write(vals)
            if self.action_id and old_total_progress_rate != record.total_progress_rate == 100.0:
                self.action_id.with_context(**ctx).run()


class ChecklistTask(models.Model):
    _name = 'checklist.task'
    _description = 'Checklist Task'

    name = fields.Char(size=128, required=True, translate=True)
    checklist_id = fields.Many2one('checklist', 'Checklist', required=True, ondelete='cascade')
    model_id = fields.Many2one('ir.model', 'Model', related='checklist_id.model_id')
    condition = fields.Char('Condition', size=256, required=True, help="object in localcontext", default='True')
    active = fields.Boolean('Active', default=True)
    action_id = fields.Many2one('ir.actions.server', 'Action')
    sequence = fields.Integer('Priority', required=True, default=15)
    active_field = fields.Boolean("Field 'Active'", related='checklist_id.active_field')
    mandatory = fields.Boolean('Required to make active object')
    field_ids = fields.One2many('checklist.task.field', 'task_id', 'Fields', required=True)

    @api.one
    def _manage_task_instances(self, records=None):
        if not records:
            records = self.env[self.model_id.model].with_context(active_test=False).search([])
        for record in records:
            condition_checked = eval(self.condition, {'object': record, 'time': time})
            task_inst = record.checklist_task_instance_ids.filtered(lambda i: i.task_id == self)
            if condition_checked and not task_inst:
                task_inst.sudo().create({'task_id': self.id, 'res_id': record.id})
            elif not condition_checked and task_inst:
                task_inst.sudo().unlink()
            if record.checklist_task_instance_ids:
                record.checklist_task_instance_ids[0].checklist_id.with_context(no_checklist=True).compute_progress_rates(record)

    @api.model
    def create(self, vals):
        task = super(ChecklistTask, self).create(vals)
        self._manage_task_instances()
        return task

    @api.multi
    def write(self, vals):
        checklists = set([task.checklist_id for task in self])
        result = super(ChecklistTask, self).write(vals)
        self._manage_task_instances()
        for checklist in checklists:  # Recompute only previous ones because new ones are recomputed in _manage_task_instances
            checklist.compute_progress_rates()
        return result

    @api.multi
    def unlink(self):
        checklists = set([task.checklist_id for task in self])
        result = super(ChecklistTask, self).unlink()
        for checklist in checklists:
            checklist.compute_progress_rates()
        return result


class ChecklistTaskField(models.Model):
    _name = 'checklist.task.field'
    _description = 'Checklist Task Field'

    name = fields.Char(size=128, required=True, translate=True)
    task_id = fields.Many2one('checklist.task', 'Task', required=True, ondelete="cascade")
    expression = fields.Text('Expression', required=True,
                             help="You can use the following variables: object, time")


class ChecklistTaskInstance(models.Model):
    _name = 'checklist.task.instance'
    _description = 'Checklist Task Instance'

    task_id = fields.Many2one('checklist.task', 'Checklist Task', required=True, ondelete='cascade')
    sequence = fields.Integer('Priority', related='task_id.sequence', store=True)
    checklist_id = fields.Many2one('checklist', 'Checklist', related='task_id.checklist_id')
    model_id = fields.Many2one('ir.model', 'Model', related='task_id.checklist_id.model_id')
    name = fields.Char(size=128, related='task_id.name')
    mandatory = fields.Boolean('Required to make record active', related='task_id.mandatory')
    res_id = fields.Integer('Resource ID', index=True, required=True)
    active = fields.Boolean(compute='_get_activity', search='_search_activity')
    field_ids_to_fill = fields.One2many('checklist.task.field', string='Fields to fill', compute='_get_activity')
    field_ids_filled = fields.One2many('checklist.task.field', string='Filled fields', compute='_get_activity')
    progress_rate = fields.Float('Progress Rate', digits=(16, 2), default=0.0)

    @api.one
    @api.depends()
    def _get_activity(self):
        self.active = self.task_id.active
        field_ids_to_fill = self.env['checklist.task.field'].browse()
        field_ids_filled = self.env['checklist.task.field'].browse()
        localdict = {'object': self.env[self.model_id.model].browse(self.res_id),
                     'time': time}
        if eval(self.task_id.condition, localdict):
            for field in self.task_id.field_ids:
                try:
                    exec "result = bool(%s)" % str(field.expression) in localdict
                    if 'result' not in localdict or not localdict['result']:
                        field_ids_to_fill |= field
                    else:
                        field_ids_filled |= field
                except:
                    pass
        else:
            self.active = False
        self.field_ids_to_fill = field_ids_to_fill
        self.field_ids_filled = field_ids_filled

    def _search_activity(self, operator, value):
        # TODO: manage task condition
        return [('task_id.active', operator, value)]
